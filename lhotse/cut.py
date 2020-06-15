from dataclasses import dataclass
from typing import Dict, List, Optional, Iterable, Any, Union
from uuid import uuid4

import numpy as np
import yaml

from lhotse.features import Features, FeatureSet, overlay_fbank, pad_shorter
from lhotse.supervision import SupervisionSegment, SupervisionSet
from lhotse.utils import Seconds, Decibels, overlaps, TimeSpan, overspans, Pathlike, asdict_nonull


# One of the design principles for Cuts is a maximally "lazy" implementation, e.g. when overlaying/mixing Cuts,
# we'd rather sum the feature matrices only after somebody actually calls "load_features". It helps to avoid
# an excessive storage size for data augmented in various ways.

@dataclass
class Cut:
    """
    A Cut is a single "segment" that we'll train on. It contains the features corresponding to
    a piece of a recording, with zero or more SupervisionSegments.
    """
    id: str
    channel: int

    # Begin and duration are needed to specify which chunk of features to load.
    start: Seconds
    duration: Seconds

    # The features can span longer than the actual cut - the Features object "knows" its start and end time
    # within the underlying recording. We can expect the interval [begin, begin + duration] to be a subset of the
    # interval represented in features.
    features: Features

    # Supervisions that will be used as targets for model training later on. They don't have to cover the whole
    # cut duration. They also might overlap.
    supervisions: List[SupervisionSegment]

    @property
    def end(self) -> Seconds:
        return self.start + self.duration

    def load_features(self, root_dir: Optional[Pathlike] = None) -> np.ndarray:
        """
        Load the features from the underlying storage and cut them to the relevant
        [begin, duration] region of the current Cut.
        Optionally specify a `root_dir` prefix to prefix the features path with.
        """
        return self.features.load(root_dir=root_dir, start=self.start, duration=self.duration)

    def truncate(
            self,
            *,
            offset: Seconds = 0.0,
            until: Optional[Seconds] = None,
            keep_excessive_supervisions: bool = True
    ) -> 'Cut':
        """
        Returns a new Cut that is a sub-region of the current Cut. The `offset` parameter controls the start of the
        new cut relative to the current Cut's start, and `until` parameter controls the new cuts end.
        Since trimming may happen inside a SupervisionSegment, the caller has an option to either keep or discard
        such supervisions with `keep_excessive_supervision` flag.
        Note that no operation is done on the actual features - it's only during load_features() when the actual
        changes happen (a subset of features is loaded).

        Example:
        >>> from math import isclose
        >>> cut = Cut(id='x', channel=0, start=3.0, duration=8.0, features='dummy', supervisions=[])
        >>> trimmed_cut = cut.truncate(offset=5.0, until=7.0)
        >>> trimmed_cut.start == 5.0 and isclose(trimmed_cut.duration, 2.0) and isclose(cut.end, 7.0)
        """
        new_start = self.start + offset
        new_duration = self.duration - new_start if until is None else until - offset
        assert new_duration > 0.0
        assert new_start + new_duration <= self.start + self.duration + 1e-5
        new_time_span = TimeSpan(start=new_start, end=new_start + new_duration)
        criterion = overlaps if keep_excessive_supervisions else overspans
        return Cut(
            id=str(uuid4()),
            channel=self.channel,
            start=new_start,
            duration=new_duration,
            supervisions=[
                segment for segment in self.supervisions if criterion(new_time_span, segment)
            ],
            features=self.features
        )

    def overlay(self, other: 'Cut', offset_other_by: Seconds = 0.0, snr: Decibels = 0.0) -> 'MixedCut':
        """
        Overlay, or mix, this Cut with the `other` Cut. Optionally the `other` Cut may be shifted by `offset_other_by`
        Seconds and scaled down (positive SNR) or scaled up (negative SNR).
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        # TODO: allow mixing more than one cut together (make MixedCut a "list" of cuts)
        return MixedCut(
            id=str(uuid4()),
            left_cut_id=self.id,
            right_cut_id=other.id,
            offset_right_by=offset_other_by,
            snr=snr
        )

    def append(self, other: 'Cut', snr: float) -> 'MixedCut':
        """
        Append the `other` Cut after the current Cut. Conceptually the same as `overlay` but with an offset
        matching the current cuts length. Optionally scale down (positive SNR) or scale up (negative SNR)
        the `other` cut.
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        return self.overlay(other=other, offset_other_by=self.duration, snr=snr)


@dataclass
class MixedCut:
    """
    Represents a Cut that's created from other Cuts via overlay or append operations.
    The actual mixing operations are performed upon loading the features into memory.
    In order to load the features, it needs to access the CutSet object that holds the "ingredient" cuts,
    as it only holds their IDs ("pointers").
    """
    # TODO: it could actually consist of more than two cuts by having a list of "ingredient" ids, offsets and snrs
    id: str
    left_cut_id: str
    right_cut_id: str
    offset_right_by: Seconds
    snr: Decibels

    def with_cut_set(self, cut_set: 'CutSet') -> 'MixedCut':
        """
        Provide the source cut set that can be looked up to resolve the MixedCut's dependencies.
        This method is a necessary, because the MixedCut acts like a "pointer" to the cuts that were used to create it.
        """
        self._cut_set = cut_set
        return self

    @property
    def supervisions(self) -> List[SupervisionSegment]:
        """Lists the supervisions of the underyling source cuts."""
        return self._cut_set.cuts[self.left_cut_id].supervisions + self._cut_set.cuts[self.right_cut_id].supervisions

    @property
    def duration(self) -> Seconds:
        return max(
            self._cut_set.cuts[self.left_cut_id].duration,
            self.offset_right_by + self._cut_set.cuts[self.right_cut_id].duration
        )

    def load_features(self, root_dir: Optional[Pathlike] = None) -> np.ndarray:
        """Loads the features of the source cuts and overlays them on-the-fly."""
        cuts = [self._cut_set.cuts[id_] for id_ in [self.left_cut_id, self.right_cut_id]]
        frame_length, frame_shift = cuts[0].features.frame_length, cuts[0].features.frame_shift
        assert frame_length == cuts[1].features.frame_length
        assert frame_shift == cuts[1].features.frame_shift
        feats = [cut.load_features(root_dir=root_dir) for cut in cuts]
        feats = pad_shorter(*feats)
        overlayed_feats = overlay_fbank(
            feats[0],
            feats[1],
            snr=self.snr,
            offset_right_by=self.offset_right_by,
            frame_length=frame_length,
            frame_shift=frame_shift
        )
        return overlayed_feats


@dataclass
class CutSet:
    """
    CutSet combines features with their corresponding supervisions.
    It may have wider span than the actual supervisions, provided the features for the whole span exist.
    It is the basic building block of PyTorch-style Datasets for speech/audio processing tasks.
    """
    cuts: Dict[str, Union[Cut, MixedCut]]

    @property
    def mixed_cuts(self) -> Dict[str, MixedCut]:
        return {id_: cut for id_, cut in self.cuts.items() if isinstance(cut, MixedCut)}

    @property
    def simple_cuts(self) -> Dict[str, Cut]:
        return {id_: cut for id_, cut in self.cuts.items() if isinstance(cut, Cut)}

    @staticmethod
    def from_yaml(path: Pathlike) -> 'CutSet':
        with open(path) as f:
            raw_cuts = yaml.safe_load(f)

        def deserialize_one(cut: Dict[str, Any]):
            cut_type = cut['type']
            del cut['type']

            if cut_type == 'MixedCut':
                return MixedCut(**cut)
            elif cut_type != 'Cut':
                raise ValueError(f"Unexpected cut type during deserialization: '{cut_type}'")

            feature_info = cut['features']
            del cut['features']
            supervision_infos = cut['supervisions']
            del cut['supervisions']
            return Cut(
                **cut,
                features=Features(**feature_info),
                supervisions=[SupervisionSegment(**s) for s in supervision_infos]
            )

        return CutSet(cuts={cut['id']: deserialize_one(cut) for cut in raw_cuts})

    def to_yaml(self, path: Pathlike):
        with open(path, 'w') as f:
            yaml.safe_dump([{**asdict_nonull(cut), 'type': type(cut).__name__} for cut in self], stream=f)

    def with_source_cuts_from(self, source: 'CutSet'):
        """
        Provide the source cut set that can be looked up to resolve the dependencies of the MixedCut's in this CutSet.
        This method is a necessary, because the MixedCut acts like a "pointer" to the cuts that were used to create it.
        """
        for cut in self.mixed_cuts.values():
            cut.with_cut_set(source)
        return self

    def __len__(self) -> int:
        return len(self.cuts)

    def __iter__(self) -> Iterable[Cut]:
        return iter(self.cuts.values())

    def __add__(self, other: 'CutSet') -> 'CutSet':
        return CutSet(cuts={**self.cuts, **other.cuts})


def make_cuts_from_supervisions(supervision_set: SupervisionSet, feature_set: FeatureSet) -> CutSet:
    """
    Utility that converts a SupervisionSet to a CutSet without any adjustment of the segment boundaries.
    It attaches the relevant features from the corresponding FeatureSet.
    """
    cuts = (
        Cut(
            id=str(uuid4()),
            channel=supervision.channel_id,
            start=supervision.start,
            duration=supervision.duration,
            features=feature_set.find(
                recording_id=supervision.recording_id,
                channel_id=supervision.channel_id,
                start=supervision.start,
                duration=supervision.duration,
            ),
            supervisions=[supervision]
        )
        for idx, supervision in enumerate(supervision_set)
    )
    return CutSet(cuts={cut.id: cut for cut in cuts})


def mix_stereo_cut_set(cut_set: CutSet) -> CutSet:
    """
    Utility that converts a CutSet that contains Cuts from both channels of a recording into a CutSet where
    these channels are downmixed into mono (by overlaying their features with 0 SNR and 0 offset).
    It assumes the CutSet is sorted by a tuple of (recording_id, channel); conceptually equivalent to:

    [
        Cut(channel=0, supervisions=[SupervisionSegment(recording_id='abcd', ...), ...], ...),
        Cut(channel=1, supervisions=[SupervisionSegment(recording_id='abcd', ...), ...], ...),
        Cut(channel=0, supervisions=[SupervisionSegment(recording_id='efgh', ...), ...], ...),
        Cut(channel=1, supervisions=[SupervisionSegment(recording_id='efgh', ...), ...], ...),
    ]
    """
    channels = list(set(c.channel for c in cut_set))
    channel0_cuts = [c for c in cut_set if c.channel == channels[0]]
    channel1_cuts = [c for c in cut_set if c.channel == channels[1]]
    mixed_cuts = (c0.overlay(c1) for c0, c1 in zip(channel0_cuts, channel1_cuts))
    return CutSet(cuts={cut.id: cut for cut in mixed_cuts})