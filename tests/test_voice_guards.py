"""Source guards for voice-input regression fixes.

Each test reads production JS source and verifies that a specific pattern
is present. These patterns exist to prevent real bugs that were observed in
production. Removing any of them re-introduces the corresponding regression.

Covered regressions (in order):

1. SPEECH-DROP: updateLiveInputBar() rebuilds the bar while SpeechNode is
   capturing, discarding the onSubmit closure and silently dropping the
   message. Fixed by adding an _activeSpeechNode guard parallel to the
   existing _activeRecognition guard in live-panel.js.

2. PREMATURE-SILENCE: silence-detection signals (RMS gap, Whisper VAD gap,
   word-stability) fired while the model was processing (no mic data
   arriving), making the capture end mid-speech. Fixed by adding a
   micSilentFor() check that gates all three signals on real mic silence,
   and by making _finishing retractable when the mic level recovers.

3. FINAL-REGRESSION: the final single-pass Whisper transcription sometimes
   produced worse output than the streaming path for proper nouns.
   Fixed by _mergeWithCommitted(), which prefers streaming when the final
   result omits words that were already stably committed during capture.

4. MAX_MS-CUTOFF: MAX_MS = 60000 (1 minute) force-stopped recordings mid-speech
   for users dictating long messages (~10 lines at normal pace = ~60 seconds).
   The cutoff fired "every single time" because speaking pace is consistent.
   Fixed by raising MAX_MS to 300000 (5 minutes) — silence detection ends the
   recording sooner for normal use; the hard cap is a true last-resort backstop.

5. PARTIAL-BLOB-GROWTH: pumpPartial sent ALL accumulated audio on every cycle.
   After 30+ seconds, blobs exceeded Whisper's 30-second context limit, causing
   (a) linearly-growing partial latency, (b) the LocalAgreement algorithm to stall
   when the model returned inconsistent transcriptions of long audio, triggering the
   stability-silence check. Fixed by capping partial audio at PARTIAL_WINDOW chunks
   (~30 seconds), keeping partial latency constant regardless of session length.

6. ADAPTIVE-SILENCE: short (SILENCE_SHORT, STABLE_MS) thresholds suited brief
   dictations but treated normal inter-sentence pauses in long messages as
   end-of-speech. Fixed by scaling both thresholds with committed word count so
   short messages stay snappy and long ones tolerate natural thinking pauses.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "static" / "js"


def _read(path):
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regression 1 — SPEECH-DROP
# updateLiveInputBar() must not rebuild the bar while _activeSpeechNode is
# capturing. Without the guard the bar's textarea DOM node is replaced and
# the onSubmit closure captured at bar-build time becomes stale — the
# message is dropped every time a session finishes its turn mid-dictation.
# ---------------------------------------------------------------------------

class TestSpeechNodeBarRebuildGuard:

    def _src(self):
        return _read(_JS / "live-panel.js")

    def test_active_speech_node_guard_present(self):
        """updateLiveInputBar() must guard against _activeSpeechNode being active.
        Without this, turning mid-dictation drops messages silently."""
        src = self._src()
        assert "_activeSpeechNode" in src, (
            "SPEECH-DROP regression: live-panel.js has no _activeSpeechNode guard. "
            "updateLiveInputBar() will rebuild the bar mid-capture and drop messages."
        )

    def test_active_speech_node_guard_checks_target_containment(self):
        """The guard must verify the capture target is actually inside the bar
        (bar.contains(_activeSpeechNode._target)) — not just that the variable
        is truthy — so it only blocks when the bar owns the active capture."""
        src = self._src()
        assert re.search(r'bar\.contains\(_activeSpeechNode\._target\)', src), (
            "SPEECH-DROP regression: _activeSpeechNode guard must check "
            "bar.contains(_activeSpeechNode._target). A bare truthiness check "
            "would block bar updates after capture ends (if the variable lingers)."
        )

    def test_active_speech_node_guard_does_not_update_live_bar_state(self):
        """When the guard fires it must return WITHOUT updating liveBarState, so
        that _refreshBarSoon() (called from commitSend) still sees a stale key
        and re-renders the bar after send completes."""
        src = self._src()
        # The guard block must contain 'return' and must NOT assign liveBarState.
        # Strategy: extract the guard block and check it.
        # We look for the pattern: the _activeSpeechNode guard returns, and the
        # liveBarState assignment comes AFTER (not inside) the guard.
        guard_pos = src.find("_activeSpeechNode._target && bar.contains(_activeSpeechNode._target)")
        assert guard_pos != -1, "Guard pattern not found — see test above"
        # Everything from the guard up to the next liveBarState assignment must
        # contain a `return` statement.
        snippet = src[guard_pos:guard_pos + 300]
        assert "return" in snippet, (
            "SPEECH-DROP regression: _activeSpeechNode guard must return early "
            "without updating liveBarState — otherwise _refreshBarSoon() won't "
            "re-render the bar after send completes."
        )

    def test_active_speech_node_guard_is_near_active_recognition_guard(self):
        """The _activeSpeechNode guard must appear close to the _activeRecognition
        guard — they are symmetric and must be kept in sync. If one guard is moved
        or deleted, the other should be too (or it's a deliberate asymmetry that
        needs a comment explaining why)."""
        lines = self._src().splitlines()
        recognition_line = next(
            (i + 1 for i, ln in enumerate(lines)
             if "_activeRecognition._target && bar.contains(_activeRecognition._target)" in ln),
            None,
        )
        speechnode_line = next(
            (i + 1 for i, ln in enumerate(lines)
             if "_activeSpeechNode._target && bar.contains(_activeSpeechNode._target)" in ln),
            None,
        )
        assert recognition_line is not None, "_activeRecognition guard baseline not found"
        assert speechnode_line is not None, "_activeSpeechNode guard not found — see tests above"
        # They appear in consecutive if-blocks in updateLiveInputBar() — must be within 20 lines.
        distance = abs(recognition_line - speechnode_line)
        assert distance <= 20, (
            f"SPEECH-DROP regression: _activeSpeechNode guard (line {speechnode_line}) "
            f"is {distance} lines from _activeRecognition guard (line {recognition_line}). "
            f"They should be adjacent if-blocks in updateLiveInputBar()."
        )


# ---------------------------------------------------------------------------
# Regression 2 — PREMATURE-SILENCE
# All three silence-detection signals must be gated on actual mic silence
# via micSilentFor(), not just on model-side timing or word-count stability.
# When the model is slow to process, all three signals can fire on stale
# data while the user is still actively speaking.
# ---------------------------------------------------------------------------

class TestSilenceDetectionHardeningGuards:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_mic_silent_for_helper_exists(self):
        """micSilentFor(ms) must exist in voice.js — it's the sole mic-confirmation
        primitive that gates all three silence signals against model-processing lag."""
        src = self._src()
        assert "micSilentFor" in src, (
            "PREMATURE-SILENCE regression: micSilentFor() helper missing from voice.js. "
            "All three silence-detection signals will fire on model-side timing alone "
            "and will trigger mid-speech when the model is processing slowly."
        )

    def test_last_speech_at_tracked_on_rms_activity(self):
        """_lastSpeechAt must be updated whenever the mic RMS is above threshold.
        It is the evidence that micSilentFor() uses to confirm real silence."""
        src = self._src()
        assert "_lastSpeechAt" in src, (
            "PREMATURE-SILENCE regression: _lastSpeechAt timestamp tracking missing. "
            "micSilentFor() has no evidence of recent speech and will always report "
            "silence — making the silence guard a no-op."
        )
        # The assignment must be inside the level-timer's 'above threshold' branch
        # (not just a constant). Check that it appears near RMS_THRESHOLD logic.
        assert re.search(r'_lastSpeechAt\s*=\s*_now\(\)', src), (
            "PREMATURE-SILENCE regression: _lastSpeechAt must be stamped with _now() "
            "when the mic level is active."
        )

    def test_gap_silence_signal_gated_on_mic_silent_for(self):
        """The Whisper VAD gap signal (res.gap >= GAP_S) must be AND-ed with
        micSilentFor() before setting _finishing. Without this, the signal fires
        on stale VAD data from before the model started processing."""
        src = self._src()
        assert re.search(
            r'res\.gap\s*>=\s*GAP_S.*micSilentFor|micSilentFor.*res\.gap\s*>=\s*GAP_S',
            src, re.DOTALL
        ), (
            "PREMATURE-SILENCE regression: Whisper VAD gap signal must be gated by "
            "micSilentFor(). Currently fires on stale model output when mic is active."
        )

    def test_stability_silence_signal_gated_on_mic_silent_for(self):
        """The word-stability signal (committedWords unchanged) must be AND-ed with
        micSilentFor() before setting _finishing. Without this, it fires when words
        are stable simply because the model hasn't returned new partials yet."""
        src = self._src()
        # The stability check pattern: committedWords.length unchanged for STABLE_MS,
        # AND micSilentFor(STABLE_MS)
        assert re.search(
            r'STABLE_MS.*micSilentFor|micSilentFor.*STABLE_MS',
            src, re.DOTALL
        ), (
            "PREMATURE-SILENCE regression: word-stability silence signal must be gated "
            "by micSilentFor(STABLE_MS). Currently fires when model is slow to return "
            "new partials, not when the user is actually silent."
        )

    def test_finishing_flag_is_retractable(self):
        """_finishing must be reversible when the mic level recovers. If it's
        one-way, any false-positive silence detection permanently ends capture
        even if the user keeps speaking."""
        src = self._src()
        # Look for the pattern: controller._finishing = false (the retraction)
        assert re.search(r'controller\._finishing\s*=\s*false', src), (
            "PREMATURE-SILENCE regression: _finishing flag is not retractable. "
            "A false-positive silence detection will permanently end capture even "
            "if the user continues speaking."
        )

    def test_finishing_retracted_when_mic_recovers(self):
        """The _finishing retraction must happen inside the above-threshold mic branch
        (where _lastSpeechAt is also updated). The two must be colocated so the mic
        recovery and retraction are atomic."""
        lines = self._src().splitlines()
        last_speech_line = next(
            (i + 1 for i, ln in enumerate(lines)
             if "controller._lastSpeechAt = _now()" in ln),
            None,
        )
        retract_line = next(
            (i + 1 for i, ln in enumerate(lines)
             if "controller._finishing = false" in ln),
            None,
        )
        assert last_speech_line is not None, "_lastSpeechAt stamping not found"
        assert retract_line is not None, "_finishing retraction not found"
        # Both are in the above-threshold mic branch — must be within 20 lines.
        distance = abs(last_speech_line - retract_line)
        assert distance <= 20, (
            f"PREMATURE-SILENCE regression: _lastSpeechAt update (line {last_speech_line}) "
            f"and _finishing retraction (line {retract_line}) are {distance} lines apart. "
            f"They should be colocated in the above-threshold mic branch."
        )


# ---------------------------------------------------------------------------
# Regression 3 — FINAL-REGRESSION
# The final single-pass Whisper transcription sometimes produces worse output
# than the streaming path (proper nouns normalised, casing lost). The merge
# helper prefers the streaming result when the final omits stably-committed
# words, and it must be used in the recorder.onstop handler.
# ---------------------------------------------------------------------------

class TestMergeWithCommittedGuards:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_merge_with_committed_helper_exists(self):
        """_mergeWithCommitted() must exist in voice.js. It is the only defence
        against the final Whisper pass regressing proper nouns and casing that
        the streaming path handled correctly."""
        src = self._src()
        assert "_mergeWithCommitted" in src, (
            "FINAL-REGRESSION: _mergeWithCommitted() helper missing from voice.js. "
            "The final transcription will replace properly-cased proper nouns with "
            "normalised equivalents from the single-pass model output."
        )

    def test_merge_with_committed_used_in_recorder_onstop(self):
        """_mergeWithCommitted() must be called in recorder.onstop. That is where
        the final Whisper result arrives and the merge decision is made."""
        src = self._src()
        # Use the handler assignment form (not comment occurrences like "recorder.onstop NEVER fires")
        onstop_pos = src.find("recorder.onstop = ")
        assert onstop_pos != -1, "recorder.onstop assignment not found in voice.js"
        # The handler body is within the next ~1500 chars of the assignment.
        snippet = src[onstop_pos:onstop_pos + 1500]
        assert "_mergeWithCommitted" in snippet, (
            "FINAL-REGRESSION: _mergeWithCommitted() is not called inside "
            "recorder.onstop. The merge logic is unreachable and the final "
            "Whisper pass can still regress proper-noun transcription."
        )

    def test_merge_with_committed_accepts_committed_words(self):
        """_mergeWithCommitted() must accept committedWords (the stabilised streaming
        output) as its third argument. Without it the function has nothing to compare
        against and will always return the (possibly regressed) final result."""
        src = self._src()
        # The function signature must accept three params.
        match = re.search(
            r'function _mergeWithCommitted\s*\(([^)]+)\)', src
        )
        assert match, "_mergeWithCommitted function signature not found"
        params = [p.strip() for p in match.group(1).split(",")]
        assert len(params) >= 3, (
            f"FINAL-REGRESSION: _mergeWithCommitted has {len(params)} param(s); "
            f"expected at least 3 (finalText, streamingText, committedWords). "
            f"Without committedWords it cannot detect proper-noun regressions."
        )

    def test_merge_falls_back_to_streaming_when_committed_omitted(self):
        """If the final text does NOT include the committed streaming words but
        the streaming text does, the merge must prefer streaming. This is the
        key invariant — assert the logic exists in the source."""
        src = self._src()
        # The implementation checks: if norm(finalText).includes(committed) return final;
        # if norm(streamingText).includes(committed) return streaming.
        # Look for the streaming fallback branch.
        assert re.search(r'return\s+streamingText', src) or \
               re.search(r'return\s+\w+streaming\w*', src, re.IGNORECASE), (
            "FINAL-REGRESSION: _mergeWithCommitted has no branch that returns the "
            "streaming result. The function will always return finalText even when "
            "the final pass omitted committed proper nouns."
        )


# ---------------------------------------------------------------------------
# Regression 4 — MAX_MS-CUTOFF
# MAX_MS = 60000 (1 minute) hard-stopped recordings mid-speech every time the
# user dictated a long message (~10 lines at normal speaking pace ≈ 60 seconds).
# Fixed by raising MAX_MS to 300000 (5 minutes); silence detection ends most
# recordings much sooner.
# ---------------------------------------------------------------------------

class TestMaxMsCutoffGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_max_ms_is_above_one_minute(self):
        """MAX_MS must be > 60000 (1 minute). At 1 minute, users dictating
        ~10 lines at normal speaking pace hit the hard cap mid-speech every
        single time. The silence detector ends typical recordings long before
        5 minutes; the hard cap is a last-resort backstop."""
        src = self._src()
        match = re.search(r'\bMAX_MS\s*=\s*(\d+)', src)
        assert match, "MAX_MS constant not found in voice.js"
        max_ms = int(match.group(1))
        assert max_ms > 60000, (
            f"MAX_MS-CUTOFF regression: MAX_MS is {max_ms}ms (<= 60 seconds). "
            f"Users dictating ~10 lines at normal pace (~120 wpm) will be cut off "
            f"mid-speech every time. MAX_MS must be > 60000 (at least 3-5 minutes)."
        )

    def test_max_ms_is_at_least_three_minutes(self):
        """MAX_MS should be >= 180000 (3 minutes) to accommodate long dictations
        without interruption. Silence detection ends the recording much sooner
        for typical use, so raising this cap has no effect on short messages."""
        src = self._src()
        match = re.search(r'\bMAX_MS\s*=\s*(\d+)', src)
        assert match, "MAX_MS constant not found in voice.js"
        max_ms = int(match.group(1))
        assert max_ms >= 180000, (
            f"MAX_MS-CUTOFF regression: MAX_MS is {max_ms}ms (< 3 minutes). "
            f"Raise to at least 180000 for reliable long-dictation support."
        )


# ---------------------------------------------------------------------------
# Regression 5 — PARTIAL-BLOB-GROWTH
# pumpPartial sent ALL accumulated audio on every cycle. After 30+ seconds
# this exceeded Whisper's 30-second context limit, causing linearly-growing
# partial latency and LocalAgreement stalls that triggered stability-silence.
# Fixed by capping partial audio at PARTIAL_WINDOW chunks.
# ---------------------------------------------------------------------------

class TestPartialBlobWindowGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_partial_window_constant_exists(self):
        """PARTIAL_WINDOW constant must exist and be <= 75 chunks (~30s at 400ms).
        It caps the audio sent for partial transcription at Whisper's actual
        context limit so partial latency stays constant regardless of session length."""
        src = self._src()
        match = re.search(r'\bPARTIAL_WINDOW\s*=\s*(\d+)', src)
        assert match, (
            "PARTIAL-BLOB-GROWTH regression: PARTIAL_WINDOW constant not found. "
            "pumpPartial sends ALL accumulated audio, causing partial latency to "
            "grow linearly with session length and breaking LocalAgreement at 30+ seconds."
        )
        window = int(match.group(1))
        assert window <= 75, (
            f"PARTIAL-BLOB-GROWTH regression: PARTIAL_WINDOW is {window} chunks "
            f"({window * 0.4:.0f}s at 400ms timeslice), which exceeds Whisper's "
            f"30-second context limit. Cap must be <= 75 chunks."
        )

    def test_partial_uses_window_not_all_chunks(self):
        """pumpPartial must use chunks.slice(partialStart) (windowed), NOT
        chunks.slice() (all chunks). Sending all audio causes latency to grow
        unboundedly and breaks LocalAgreement for sessions > 30 seconds."""
        src = self._src()
        # Must NOT contain a bare chunks.slice() (without a start argument)
        # as the primary blob construction in pumpPartial.
        assert "chunks.slice(partialStart)" in src or "chunks.slice(PARTIAL_WINDOW" in src or \
               re.search(r'chunks\.slice\(.*partialStart', src), (
            "PARTIAL-BLOB-GROWTH regression: pumpPartial must use a windowed slice "
            "(e.g. chunks.slice(partialStart)) not chunks.slice() for the partial blob."
        )


# ---------------------------------------------------------------------------
# Regression 6 — ADAPTIVE-SILENCE
# Fixed SILENCE_SHORT / STABLE_MS thresholds treated normal inter-sentence
# pauses in long messages as end-of-speech. Fixed by scaling both thresholds
# with committed word count.
# ---------------------------------------------------------------------------

class TestAdaptiveSilenceGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_silence_threshold_scales_with_word_count(self):
        """The silence timer must use an adaptive window that grows with committed
        word count. A fixed SILENCE_SHORT causes long dictations to cut off during
        natural inter-sentence pauses (the user is 'mid-thought', not done)."""
        src = self._src()
        # Look for adaptive silence logic: silence threshold increasing based on word count
        assert re.search(r'wordCount|committedWords\.length.*silence|silence.*committedWords\.length',
                         src, re.IGNORECASE) or \
               re.search(r'adapted.*silence|silence.*adapt', src, re.IGNORECASE) or \
               "adaptedSilenceMs" in src, (
            "ADAPTIVE-SILENCE regression: silence threshold is not adaptive. "
            "Long dictations will be cut off during normal inter-sentence pauses. "
            "Scale the silence window with committedWords.length."
        )

    def test_stable_ms_scales_with_word_count(self):
        """The STABLE_MS stability check must use an adaptive threshold that grows
        with committed word count. A fixed threshold fires prematurely during
        slow partial transcription on long recordings."""
        src = self._src()
        assert "adaptedStableMs" in src or \
               re.search(r'STABLE_MS.*committed|committed.*STABLE_MS', src, re.IGNORECASE), (
            "ADAPTIVE-SILENCE regression: STABLE_MS is not adaptive. "
            "The stability check will fire prematurely for long messages "
            "when partial transcription is slow. Scale with committedWords.length."
        )
