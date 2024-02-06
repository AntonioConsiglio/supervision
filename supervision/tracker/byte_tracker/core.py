from typing import List, Optional, Tuple

import numpy as np

from supervision.detection.core import Detections
from supervision.tracker.byte_tracker import matching
from supervision.tracker.byte_tracker.basetrack import BaseTrack, TrackState
from supervision.tracker.byte_tracker.kalman_filter import KalmanFilter


class STrack(BaseTrack):
    shared_kalman = KalmanFilter()

    def __init__(self, tlwh, score, class_ids):
        # wait activate
        self._tlwh = np.asarray(tlwh, dtype=np.float32)
        self.kalman_filter = None
        self.mean, self.covariance = None, None
        self.is_activated = False

        self.score = score
        self.class_ids = class_ids
        self.tracklet_len = 0

    def predict(self):
        mean_state = self.mean.copy()
        if self.state != TrackState.Tracked:
            mean_state[7] = 0
        self.mean, self.covariance = self.kalman_filter.predict(
            mean_state, self.covariance
        )

    @staticmethod
    def multi_predict(stracks):
        if len(stracks) > 0:
            multi_mean = []
            multi_covariance = []
            for i, st in enumerate(stracks):
                multi_mean.append(st.mean.copy())
                multi_covariance.append(st.covariance)
                if st.state != TrackState.Tracked:
                    multi_mean[i][7] = 0

            multi_mean, multi_covariance = STrack.shared_kalman.multi_predict(
                np.asarray(multi_mean), np.asarray(multi_covariance)
            )
            for i, (mean, cov) in enumerate(zip(multi_mean, multi_covariance)):
                stracks[i].mean = mean
                stracks[i].covariance = cov

    def activate(self, kalman_filter, frame_id):
        """Start a new tracklet"""
        self.kalman_filter = kalman_filter
        self.track_id = self.next_id()
        self.mean, self.covariance = self.kalman_filter.initiate(
            self.tlwh_to_xyah(self._tlwh)
        )

        self.tracklet_len = 0
        self.state = TrackState.Tracked
        if frame_id == 1:
            self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id

    def re_activate(self, new_track, frame_id, new_id=False):
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_track.tlwh)
        )
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()
        self.score = new_track.score

    def update(self, new_track, frame_id):
        """
        Update a matched track
        :type new_track: STrack
        :type frame_id: int
        :type update_feature: bool
        :return:
        """
        self.frame_id = frame_id
        self.tracklet_len += 1

        new_tlwh = new_track.tlwh
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_tlwh)
        )
        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score

    @property
    def tlwh(self):
        """Get current position in bounding box format `(top left x, top left y,
        width, height)`.
        """
        if self.mean is None:
            return self._tlwh.copy()
        ret = self.mean[:4].copy()
        ret[2] *= ret[3]
        ret[:2] -= ret[2:] / 2
        return ret

    @property
    def tlbr(self):
        """Convert bounding box to format `(min x, min y, max x, max y)`, i.e.,
        `(top left, bottom right)`.
        """
        ret = self.tlwh.copy()
        ret[2:] += ret[:2]
        return ret

    @staticmethod
    def tlwh_to_xyah(tlwh):
        """Convert bounding box to format `(center x, center y, aspect ratio,
        height)`, where the aspect ratio is `width / height`.
        """
        ret = np.asarray(tlwh).copy()
        ret[:2] += ret[2:] / 2
        ret[2] /= ret[3]
        return ret

    def to_xyah(self):
        return self.tlwh_to_xyah(self.tlwh)

    @staticmethod
    def tlbr_to_tlwh(tlbr):
        ret = np.asarray(tlbr).copy()
        ret[2:] -= ret[:2]
        return ret

    @staticmethod
    def tlwh_to_tlbr(tlwh):
        ret = np.asarray(tlwh).copy()
        ret[2:] += ret[:2]
        return ret

    def __repr__(self):
        return "OT_{}_({}-{})".format(self.track_id, self.start_frame, self.end_frame)


def detections2boxes(detections: Detections) -> np.ndarray:
    """
    Convert Supervision Detections to numpy tensors for further computation.
    Args:
        detections (Detections): Detections/Targets in the format of sv.Detections.
    Returns:
        (np.ndarray): Detections as numpy tensors as in
            `(x_min, y_min, x_max, y_max, confidence, class_id)` order.
    """
    return np.hstack(
        (
            detections.xyxy,
            detections.confidence[:, np.newaxis],
            detections.class_id[:, np.newaxis],
        )
    )


class ByteTrack:
    """
    Initialize the ByteTrack object.

    <video controls>
        <source src="https://media.roboflow.com/supervision/video-examples/how-to/track-objects/annotate-video-with-traces.mp4" type="video/mp4">
    </video>

    Parameters:
        track_thresh (float, optional): Detection confidence threshold
            for track activation.
        track_buffer (int, optional): Number of frames to buffer when a track is lost.
        match_thresh (float, optional): Threshold for matching tracks with detections.
        frame_rate (int, optional): The frame rate of the video.
    """  # noqa: E501 // docs

    def __init__(
        self,
        track_thresh: float = 0.25,
        track_buffer: int = 30,
        match_thresh: float = 0.8,
        frame_rate: int = 30,
    ):
        self.track_thresh = track_thresh
        self.match_thresh = match_thresh

        self.frame_id = 0
        self.det_thresh = self.track_thresh + 0.1
        self.max_time_lost = int(frame_rate / 30.0 * track_buffer)
        self.kalman_filter = KalmanFilter()

        self.tracked_tracks: List[STrack] = []
        self.lost_tracks: List[STrack] = []
        self.removed_tracks: List[STrack] = []
    
    def update_with_tensors(self, tensors: np.ndarray) -> List[STrack]:
        """
        Updates the tracker with the provided tensors and returns the updated tracks.

        Parameters:
            tensors: The new tensors to update with.

        Returns:
            List[STrack]: Updated tracks.
        """
        self.frame_id += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        class_ids = tensors[:, 5]
        scores = tensors[:, 4]
        bboxes = tensors[:, :4]

        remain_inds = scores > self.track_thresh
        inds_low = scores > 0.1
        inds_high = scores < self.track_thresh

        inds_second = np.logical_and(inds_low, inds_high)
        dets_second = bboxes[inds_second]
        dets = bboxes[remain_inds]
        scores_keep = scores[remain_inds]
        scores_second = scores[inds_second]

        class_ids_keep = class_ids[remain_inds]
        class_ids_second = class_ids[inds_second]

        if len(dets) > 0:
            """Detections"""
            detections = [
                STrack(STrack.tlbr_to_tlwh(tlbr), s, c)
                for (tlbr, s, c) in zip(dets, scores_keep, class_ids_keep)
            ]
        else:
            detections = []

        """ Add newly detected tracklets to tracked_stracks"""
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]

        for track in self.tracked_tracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        """ Step 2: First association, with high score detection boxes"""
        strack_pool = joint_tracks(tracked_stracks, self.lost_tracks)
        # Predict the current location with KF
        STrack.multi_predict(strack_pool)
        dists = matching.iou_distance(strack_pool, detections)

        dists = matching.fuse_score(dists, detections)
        matches, u_track, u_detection = matching.linear_assignment(
            dists, thresh=self.match_thresh
        )

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        """ Step 3: Second association, with low score detection boxes"""
        # association the untrack to the low score detections
        if len(dets_second) > 0:
            """Detections"""
            detections_second = [
                STrack(STrack.tlbr_to_tlwh(tlbr), s, c)
                for (tlbr, s, c) in zip(dets_second, scores_second, class_ids_second)
            ]
        else:
            detections_second = []
        r_tracked_stracks = [
            strack_pool[i]
            for i in u_track
            if strack_pool[i].state == TrackState.Tracked
        ]
        dists = matching.iou_distance(r_tracked_stracks, detections_second)
        matches, u_track, u_detection_second = matching.linear_assignment(
            dists, thresh=0.5
        )
        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        """Deal with unconfirmed tracks, usually tracks with only one beginning frame"""
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)

        dists = matching.fuse_score(dists, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(
            dists, thresh=0.7
        )
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate(self.kalman_filter, self.frame_id)
            activated_starcks.append(track)
        """ Step 5: Update state"""
        for track in self.lost_tracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        self.tracked_tracks = [
            t for t in self.tracked_tracks if t.state == TrackState.Tracked
        ]
        self.tracked_tracks = joint_tracks(self.tracked_tracks, activated_starcks)
        self.tracked_tracks = joint_tracks(self.tracked_tracks, refind_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.tracked_tracks)
        self.lost_tracks.extend(lost_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.removed_tracks)
        self.removed_tracks.extend(removed_stracks)
        self.tracked_tracks, self.lost_tracks = remove_duplicate_tracks(
            self.tracked_tracks, self.lost_tracks
        )
        output_stracks = [track for track in self.tracked_tracks if track.is_activated]

        return output_stracks

    def update_with_detections(self, detections: Detections, 
                               keep_all: bool = False) -> Detections:
        """
        Updates the tracker with the provided detections and returns the updated
        detection results.

        Args:
            detections (Detections): The detections to pass through the tracker.
            keep_all (bool): Keep detection without tracker ID.

        Example:
            ```python
            import supervision as sv
            from ultralytics import YOLO

            model = YOLO(<MODEL_PATH>)
            tracker = sv.ByteTrack()

            bounding_box_annotator = sv.BoundingBoxAnnotator()
            label_annotator = sv.LabelAnnotator()

            def callback(frame: np.ndarray, index: int) -> np.ndarray:
                results = model(frame)[0]
                detections = sv.Detections.from_ultralytics(results)
                detections = tracker.update_with_detections(detections)

                labels = [f"#{tracker_id}" for tracker_id in detections.tracker_id]

                annotated_frame = bounding_box_annotator.annotate(
                    scene=frame.copy(), detections=detections)
                annotated_frame = label_annotator.annotate(
                    scene=annotated_frame, detections=detections, labels=labels)
                return annotated_frame

            sv.process_video(
                source_path=<SOURCE_VIDEO_PATH>,
                target_path=<TARGET_VIDEO_PATH>,
                callback=callback
            )
            ```
        """
        detections = self._update_detections(detections)
        if not keep_all:
            return detections[detections.tracker_id != None]
        return detections
    
    def _update_detections(self, detections: Detections) -> Detections:
        """
        Updates the tracker with the provided Detections and returns the 
        updated Detections.

        Parameters:
            detections: Detections class with all the detections.

        Returns:
            Detections: Updated Detections with track IDs.
        """
        self.frame_id += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        # Create tensors from detections to use during track match
        tensors = detections2boxes(detections=detections)
        # Separate detections based on scores
        detection_index = np.array([j for j in range(len(tensors))])

        class_ids = tensors[:, 5]
        scores = tensors[:, 4]
        bboxes = tensors[:, :4]

        # Filter out detections below track threshold
        remain_inds = scores > self.track_thresh
        inds_low = scores > 0.1
        inds_high = scores < self.track_thresh

        inds_second = np.logical_and(inds_low, inds_high)
        dets_second = bboxes[inds_second]
        dets = bboxes[remain_inds]

        scores_keep = scores[remain_inds]
        scores_second = scores[inds_second]

        class_ids_keep = class_ids[remain_inds]
        class_ids_second = class_ids[inds_second]

        low_score_det_idx = detection_index[inds_second]

        # Track process begin here
        detections_to_track = []
        if len(dets) > 0:
            # Convert valid detections to STrack objects
            detections_to_track = [
                STrack(STrack.tlbr_to_tlwh(tlbr), s, c)
                for (tlbr, s, c) in zip(dets, scores_keep, class_ids_keep)
            ]
        """ Add newly detected tracklets to tracked_stracks"""
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]

        for track in self.tracked_tracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        """ Step 2: First association, with high score detection boxes"""
        strack_pool = joint_tracks(tracked_stracks, self.lost_tracks)
        # Predict the current location with KF
        STrack.multi_predict(strack_pool)
        dists = matching.iou_distance(strack_pool, detections_to_track)

        dists = matching.fuse_score(dists, detections_to_track)
        matches_keep, u_track, u_detection = matching.linear_assignment(
            dists, thresh=self.match_thresh
        )
        for (
            itracked,
            idet,
        ) in matches_keep:  # tracking object index and new detection index
            track = strack_pool[itracked]
            det = detections_to_track[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        """ Step 3: Second association, with low score detection boxes"""
        # association the untrack to the low score detections
        detections_second = []
        if len(dets_second) > 0:
            """Detections"""
            detections_second = [
                STrack(STrack.tlbr_to_tlwh(tlbr), s, c)
                for (tlbr, s, c) in zip(dets_second, scores_second, class_ids_second)
            ]
        # remaining sTraks
        r_tracked_stracks = [
            strack_pool[i]
            for i in u_track
            if strack_pool[i].state == TrackState.Tracked
        ]
        dists = matching.iou_distance(r_tracked_stracks, detections_second)
        # u_detection_second: Detection with low score and without match
        matches_second, u_track_second, u_detection_second = matching.linear_assignment(
            dists, thresh=0.5
        )

        # Update tracks based on low score matches
        for itracked, idet in matches_second:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        for it in u_track_second:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        """Deal with unconfirmed tracks, usually tracks with only one beginning frame"""
        detections_u_to_track = [detections_to_track[i] for i in u_detection]
        detections_u_map = {
            k: i for k, i in enumerate(u_detection)
        }  # track id: detection id
        dists = matching.iou_distance(unconfirmed, detections_u_to_track)

        dists = matching.fuse_score(dists, detections_u_to_track)
        (
            matches_unconfirmed,
            u_unconfirmed,
            u_detection_unconf,
        ) = matching.linear_assignment(dists, thresh=0.7)

        # Update unconfirmed tracks
        for itracked, idet in matches_unconfirmed:
            unconfirmed[itracked].update(detections_u_to_track[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection_unconf:
            track = detections_u_to_track[inew]
            if track.score < self.det_thresh:
                continue
            track.activate(self.kalman_filter, self.frame_id)
            activated_starcks.append(track)
        """ Step 5: Update state"""
        for track in self.lost_tracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # Update track lists
        self.tracked_tracks = [
            t for t in self.tracked_tracks if t.state == TrackState.Tracked
        ]
        self.tracked_tracks = joint_tracks(self.tracked_tracks, activated_starcks)
        self.tracked_tracks = joint_tracks(self.tracked_tracks, refind_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.tracked_tracks)
        self.lost_tracks.extend(lost_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.removed_tracks)
        self.removed_tracks.extend(removed_stracks)
        self.tracked_tracks, self.lost_tracks = remove_duplicate_tracks(
            self.tracked_tracks, self.lost_tracks
        )

        # Initialize the new track IDs for detections
        tracked_ids = [None] * len(detections)

        # Update IDs list with ID from high_score tracked detection
        if len(matches_keep) > 0:
            for k, idx in matches_keep:
                tracked_ids[idx] = self._get_track_id(strack_pool[k])
        # Update IDs list with ID from low_score tracked detection
        if len(matches_second) > 0:
            for k, idx in matches_second:
                tracked_ids[low_score_det_idx[idx]] = self._get_track_id(
                    r_tracked_stracks[k]
                )

        # Update IDs list with ID from unconfirmed tracked detection
        if len(matches_unconfirmed) > 0:
            for k, idx in matches_unconfirmed:
                tracked_ids[detections_u_map[idx]] = self._get_track_id(unconfirmed[k])

        # Update IDs list with ID from new detection
        if len(u_detection_unconf) > 0:
            for k in u_detection_unconf:
                tracked_ids[detections_u_map[k]] = self._get_track_id(
                    detections_u_to_track[k]
                )

        return self._update_detection_track_id(detections, tracked_ids)

    def _get_track_id(self, strack: STrack):
        return strack.track_id if hasattr(strack, "track_id") else None

    def _update_detection_track_id(
        self, detections: Detections, track_ids_list: List[Optional[int]]
    ):
        """
        Update the detection tracker ID.

        Parameters:
            detections: Detections object.
            strack: List of track IDs.

        Returns:
            Updated detections object.
        """
        detections.tracker_id = np.array(track_ids_list)
        return detections


def joint_tracks(
    track_list_a: List[STrack], track_list_b: List[STrack]
) -> List[STrack]:
    """
    Joins two lists of tracks, ensuring that the resulting list does not
    contain tracks with duplicate track_id values.

    Parameters:
        track_list_a: First list of tracks (with track_id attribute).
        track_list_b: Second list of tracks (with track_id attribute).

    Returns:
        Combined list of tracks from track_list_a and track_list_b
            without duplicate track_id values.
    """
    seen_track_ids = set()
    result = []

    for track in track_list_a + track_list_b:
        if track.track_id not in seen_track_ids:
            seen_track_ids.add(track.track_id)
            result.append(track)

    return result


def sub_tracks(track_list_a: List, track_list_b: List) -> List[int]:
    """
    Returns a list of tracks from track_list_a after removing any tracks
    that share the same track_id with tracks in track_list_b.

    Parameters:
        track_list_a: List of tracks (with track_id attribute).
        track_list_b: List of tracks (with track_id attribute) to
            be subtracted from track_list_a.
    Returns:
        List of remaining tracks from track_list_a after subtraction.
    """
    tracks = {track.track_id: track for track in track_list_a}
    track_ids_b = {track.track_id for track in track_list_b}

    for track_id in track_ids_b:
        tracks.pop(track_id, None)

    return list(tracks.values())


def remove_duplicate_tracks(tracks_a: List, tracks_b: List) -> Tuple[List, List]:
    pairwise_distance = matching.iou_distance(tracks_a, tracks_b)
    matching_pairs = np.where(pairwise_distance < 0.15)

    duplicates_a, duplicates_b = set(), set()
    for track_index_a, track_index_b in zip(*matching_pairs):
        time_a = tracks_a[track_index_a].frame_id - tracks_a[track_index_a].start_frame
        time_b = tracks_b[track_index_b].frame_id - tracks_b[track_index_b].start_frame
        if time_a > time_b:
            duplicates_b.add(track_index_b)
        else:
            duplicates_a.add(track_index_a)

    result_a = [
        track for index, track in enumerate(tracks_a) if index not in duplicates_a
    ]
    result_b = [
        track for index, track in enumerate(tracks_b) if index not in duplicates_b
    ]

    return result_a, result_b
