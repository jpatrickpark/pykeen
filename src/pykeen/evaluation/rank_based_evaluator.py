# -*- coding: utf-8 -*-

"""Implementation of ranked based evaluator."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import numpy as np
import torch
from dataclasses_json import dataclass_json

from .evaluator import Evaluator, MetricResults
from ..typing import MappedTriples

__all__ = [
    'compute_rank_from_scores',
    'RankBasedEvaluator',
    'RankBasedMetricResults',
]

logger = logging.getLogger(__name__)

RANK_BEST = 'best'
RANK_WORST = 'worst'
RANK_AVERAGE = 'avg'
RANK_TYPES = {RANK_BEST, RANK_WORST, RANK_AVERAGE}
RANK_AVERAGE_ADJUSTED = 'adj'


def compute_rank_from_scores(
    true_score: torch.FloatTensor,
    all_scores: torch.FloatTensor,
) -> Dict[str, torch.FloatTensor]:
    """Compute rank and adjusted rank given scores.

    :param true_score: torch.Tensor, shape: (batch_size, 1)
        The score of the true triple.
    :param all_scores: torch.Tensor, shape: (batch_size, num_entities)
        The scores of all corrupted triples (including the true triple).
    :return: a dictionary
        {
            'best': best_rank,
            'worst': worst_rank,
            'avg': avg_rank,
            'adj': adj_rank,
        }

        where

        best_rank: shape: (batch_size,)
            The best rank is the rank when assuming all options with an equal score are placed behind the current
            test triple.
        worst_rank:
            The worst rank is the rank when assuming all options with an equal score are placed in front of current
            test triple.
        avg_rank:
            The average rank is the average of the best and worst rank, and hence the expected rank over all
            permutations of the elements with the same score as the currently considered option.
        adj_rank: shape: (batch_size,)
            The adjusted rank normalises the average rank by the expected rank a random scoring would
            achieve, which is (#number_of_options + 1)/2
    """
    # The best rank is the rank when assuming all options with an equal score are placed behind the currently
    # considered. Hence, the rank is the number of options with better scores, plus one, as the rank is one-based.
    best_rank = (all_scores > true_score).sum(dim=1) + 1

    # The worst rank is the rank when assuming all options with an equal score are placed in front of the currently
    # considered. Hence, the rank is the number of options which have at least the same score minus one (as the
    # currently considered option in included in all options). As the rank is one-based, we have to add 1, which
    # nullifies the "minus 1" from before.
    worst_rank = (all_scores >= true_score).sum(dim=1)

    # The average rank is the average of the best and worst rank, and hence the expected rank over all permutations of
    # the elements with the same score as the currently considered option.
    average_rank = (best_rank + worst_rank).float() * 0.5

    # We set values which should be ignored to NaN, hence the number of options which should be considered is given by
    number_of_options = torch.isfinite(all_scores).sum(dim=1).float()

    # The expected rank of a random scoring
    expected_rank = 0.5 * (number_of_options + 1)

    # The adjusted ranks is normalized by the expected rank of a random scoring
    adjusted_average_rank = average_rank / expected_rank
    # TODO adjusted_worst_rank
    # TODO adjusted_best_rank

    return {
        RANK_BEST: best_rank,
        RANK_WORST: worst_rank,
        RANK_AVERAGE: average_rank,
        RANK_AVERAGE_ADJUSTED: adjusted_average_rank,
    }


@dataclass_json
@dataclass
class RankBasedMetricResults(MetricResults):
    """Results from computing metrics.

    Includes results from:

    - Mean Rank (MR)
    - Mean Reciprocal Rank (MRR)
    - Adjusted Mean Rank (AMR; [berrendorf2020]_)
    - Hits @ K
    """

    #: The mean over all ranks: mean_i r_i. Lower is better.
    mean_rank: Dict[str, float] = field(metadata=dict(doc='The mean over all ranks: mean_i r_i. Lower is better.'))

    #: The mean over all reciprocal ranks: mean_i (1/r_i). Higher is better.
    mean_reciprocal_rank: Dict[str, float] = field(metadata=dict(
        doc='The mean over all reciprocal ranks: mean_i (1/r_i). Higher is better.',
    ))

    #: The hits at k for different values of k, i.e. the relative frequency of ranks not larger than k.
    #: Higher is better.
    hits_at_k: Dict[str, Dict[int, float]] = field(metadata=dict(
        doc='The hits at k for different values of k, i.e. the relative frequency of ranks not larger than k.'
            ' Higher is better.',
    ))

    #: The mean over all chance-adjusted ranks: mean_i (2r_i / (num_entities+1)). Lower is better.
    #: Described by [berrendorf2020]_.
    adjusted_mean_rank: float = field(metadata=dict(
        doc='The mean over all chance-adjusted ranks: mean_i (2r_i / (num_entities+1)). Lower is better.',
    ))

    def get_metric(self, name: str) -> float:  # noqa: D102
        if name == 'adjusted_mean_rank':
            return self.adjusted_mean_rank

        dot_count = name.count('.')
        if 0 == dot_count:  # assume average by default
            rank_type, metric = 'avg', name
        elif 1 == dot_count:
            rank_type, metric = name.split('.')
        else:
            raise ValueError(f'Malformed metric name: {name}')

        if rank_type not in RANK_TYPES:
            raise ValueError(f'Invalid rank type: {rank_type}')

        if metric in {'mean_rank', 'mean_reciprocal_rank'}:
            return getattr(self, metric)[rank_type]

        rank_type_hits_at_k = self.hits_at_k[rank_type]
        for prefix in ('hits_at_', 'hits@'):
            if not metric.startswith(prefix):
                continue
            k = metric[len(prefix):]
            k = 10 if k == 'k' else int(k)
            return rank_type_hits_at_k[k]

        raise ValueError(f'Invalid metric name: {name}')

    def to_flat_dict(self):  # noqa: D102
        r = {
            'avg.adjusted_mean_rank': self.adjusted_mean_rank,
        }
        for rank_type in RANK_TYPES:
            r[f'{rank_type}.mean_rank'] = self.mean_rank[rank_type]
            r[f'{rank_type}.mean_reciprocal_rank'] = self.mean_reciprocal_rank[rank_type]
            for k, v in self.hits_at_k[rank_type].items():
                r[f'{rank_type}.hits_at_{k}'] = v
        return r


class RankBasedEvaluator(Evaluator):
    """A rank-based evaluator for KGE models.

    Calculates:

    - Mean Rank (MR)
    - Mean Reciprocal Rank (MRR)
    - Adjusted Mean Rank (AMR; [berrendorf2020]_)
    - Hits @ K
    """

    def __init__(
        self,
        ks: Optional[Iterable[int]] = None,
        filtered: bool = True,
    ):
        """Initialize rank-based evaluator.

        :param ks:
            The values for which to calculate hits@k. Defaults to {1,3,5,10}.
        :param filtered:
            Whether to use the filtered evaluation protocol. If enabled, ranking another true triple higher than the
            currently considered one will not decrease the score.
        """
        super().__init__(filtered=filtered)
        self.ks = tuple(ks) if ks is not None else (1, 3, 5, 10)
        self.ranks: Dict[str, List[float]] = defaultdict(list)

    def _update_ranks_(
        self,
        true_scores: torch.FloatTensor,
        all_scores: torch.FloatTensor,
    ) -> None:
        """Shared code for updating the stored ranks for head/tail scores.

        :param true_scores: shape: (batch_size,)
        :param all_scores: shape: (batch_size, num_entities)
        """
        batch_ranks = compute_rank_from_scores(
            true_score=true_scores,
            all_scores=all_scores,
        )
        for k, v in batch_ranks.items():
            self.ranks[k].extend(v.detach().cpu().tolist())

    def process_tail_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.BoolTensor] = None,
    ) -> None:  # noqa: D102
        self._update_ranks_(true_scores=true_scores, all_scores=scores)

    def process_head_scores_(
        self,
        hrt_batch: MappedTriples,
        true_scores: torch.FloatTensor,
        scores: torch.FloatTensor,
        dense_positive_mask: Optional[torch.BoolTensor] = None,
    ) -> None:  # noqa: D102
        self._update_ranks_(true_scores=true_scores, all_scores=scores)

    def finalize(self) -> RankBasedMetricResults:  # noqa: D102
        mean_rank = {}
        mean_reciprocal_rank = {}
        hits_at_k = {}

        for rank_type in RANK_TYPES:
            ranks = np.asarray(self.ranks.get(rank_type), dtype=np.float64)
            hits_at_k[rank_type] = {
                k: np.mean(ranks <= k)
                for k in self.ks
            }
            mean_rank[rank_type] = np.mean(ranks)
            mean_reciprocal_rank[rank_type] = np.mean(np.reciprocal(ranks))

        adjusted_ranks = np.asarray(self.ranks.get(RANK_AVERAGE_ADJUSTED), dtype=np.float64)
        adjusted_mean_rank = np.mean(adjusted_ranks)

        # Clear buffers
        self.ranks.clear()

        return RankBasedMetricResults(
            mean_rank=mean_rank,
            mean_reciprocal_rank=mean_reciprocal_rank,
            hits_at_k=hits_at_k,
            adjusted_mean_rank=adjusted_mean_rank
        )
