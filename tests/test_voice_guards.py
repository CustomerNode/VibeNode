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
        # The handler body is within the next ~3000 chars of the assignment.
        # (Increased from 1500: the adaptive watchdog comment above transcribeBlob
        # is intentionally detailed and pushed _mergeWithCommitted past the old window.
        # Increased from 2000: the BACKSTOP-PREEMPT fix (regression 10) retires the
        # finalize-time backstop at the top of onstop with its own explanatory comment.)
        snippet = src[onstop_pos:onstop_pos + 3000]
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


# ---------------------------------------------------------------------------
# Regression 7 — WATCHDOG-TIMEOUT
# A fixed 6000ms watchdog fires before Whisper finishes processing recordings
# longer than ~12s, especially when _INFER_LOCK contention (a partial still
# running when the final fires) consumes most of that budget. Fixed by scaling
# the watchdog with chunks.length so each recording gets proportional time.
# ---------------------------------------------------------------------------

class TestAdaptiveWatchdogGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_watchdog_scales_with_chunk_count(self):
        """Final transcription watchdog must scale with chunks.length.
        A fixed 6000ms fires before Whisper finishes for 30+ second recordings,
        especially when an in-flight partial is blocking _INFER_LOCK."""
        src = self._src()
        assert re.search(
            r'watchdogMs.*chunks\.length|chunks\.length.*watchdog',
            src, re.DOTALL | re.IGNORECASE
        ) or (
            "watchdogMs" in src and "chunks.length" in src
        ), (
            "WATCHDOG-TIMEOUT regression: watchdog must scale with chunks.length. "
            "A fixed 6s watchdog drops messages for any recording longer than ~12s "
            "(lock-wait time + Whisper inference time regularly exceeds 6s on CPU)."
        )

    def test_watchdog_minimum_is_at_least_10s(self):
        """Minimum watchdog must be >= 10000ms. At 6s, lock-wait alone (partial
        holds _INFER_LOCK for up to 3s) plus a short-clip inference (up to 6s)
        already exceeds the budget even for a 5-second recording."""
        src = self._src()
        match = re.search(r'watchdogMs\s*=.*?Math\.max\((\d+)', src)
        if not match:
            match = re.search(r'Math\.max\((\d+),.*?watchdog', src)
        assert match, "watchdogMs / Math.max pattern not found — see test above"
        min_ms = int(match.group(1))
        assert min_ms >= 10000, (
            f"WATCHDOG-TIMEOUT regression: watchdog minimum is {min_ms}ms (< 10s). "
            f"Lock-wait + inference regularly exceeds 6s. Minimum must be >= 10000ms."
        )

    def test_watchdog_cap_exists(self):
        """Watchdog must have a reasonable upper cap (Math.min). Without it, a
        5-minute recording produces a 150s watchdog that holds the caption open
        long after the user expected a response."""
        src = self._src()
        assert re.search(r'Math\.min\(\d+,.*watchdogMs|watchdogMs.*Math\.min\(\d+', src) or \
               re.search(r'Math\.min\(60000|Math\.min\(45000|Math\.min\(30000', src), (
            "WATCHDOG-TIMEOUT regression: no upper cap on watchdog. A recording-length "
            "proportional watchdog without a cap will stay open for minutes on very long "
            "recordings. Add Math.min(60000, ...) to cap it reasonably."
        )


# ---------------------------------------------------------------------------
# Regression 8 — STREAMING-RESET
# When the PARTIAL_WINDOW (75 chunks, ~30s) shifts forward, LocalAgreement
# resets prevWords and committedWords. Before this fix, controller._lastFullText
# was set from the NEW window's text only, so the watchdog fallback silently
# lost the first N paragraphs of a long dictation. Fixed by promoting committed
# words to controller._permanentPrefix before the reset, so _lastFullText
# always reflects the full accumulated dictation.
# ---------------------------------------------------------------------------

class TestPermanentPrefixGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_permanent_prefix_field_exists(self):
        """controller._permanentPrefix must exist in voice.js.
        Without it, every PARTIAL_WINDOW shift (every ~30s of audio) silently
        loses the previously-committed text from _lastFullText, causing the
        watchdog fallback to return only the current window's fragment."""
        src = self._src()
        assert "_permanentPrefix" in src, (
            "STREAMING-RESET regression: controller._permanentPrefix not found. "
            "The PARTIAL_WINDOW reset (every ~30s) clears committedWords without saving "
            "them — _lastFullText loses all text from prior windows on watchdog fallback."
        )

    def test_permanent_prefix_saved_on_window_shift(self):
        """committedWords must be appended to _permanentPrefix before the
        PARTIAL_WINDOW reset. If committed words are discarded without saving,
        the permanent prefix stays empty and the regression is unfixed."""
        src = self._src()
        # The save must happen INSIDE the `partialStart > _lastPartialStart` block
        # (the window-shift guard). Check that _permanentPrefix assignment is
        # near the committedWords reset.
        shift_pos = src.find("partialStart > (controller._lastPartialStart")
        assert shift_pos != -1, "partialStart window-shift check not found"
        # (Window increased from 600: the WINDOW-HEADER fix (regression 9) expanded
        # the promotion comment — it now documents why prevWords, not just
        # committedWords, is promoted at each segment seam.)
        snippet = src[shift_pos:shift_pos + 1200]
        assert "_permanentPrefix" in snippet, (
            "STREAMING-RESET regression: _permanentPrefix is not saved inside the "
            "window-shift block. committedWords are cleared without being promoted, "
            "so prior dictation is lost from _lastFullText when the window advances."
        )

    def test_permanent_prefix_included_in_last_full_text(self):
        """controller._lastFullText must concatenate _permanentPrefix with the
        current window text. Without this, the watchdog fallback only returns
        the most recent 30 seconds of a long recording."""
        src = self._src()
        # applyPartial must build _lastFullText from both perm and cur
        assert re.search(
            r'_permanentPrefix.*_lastFullText|_lastFullText.*_permanentPrefix',
            src, re.DOTALL
        ), (
            "STREAMING-RESET regression: _lastFullText does not include _permanentPrefix. "
            "On watchdog fallback, the user loses everything before the last window shift."
        )

    def test_permanent_prefix_shown_in_caption(self):
        """The live caption must include permanent prefix words in the committed
        (solid) section. Without this, the caption appears to reset and lose
        committed text every 30 seconds, making streaming look broken."""
        src = self._src()
        # _snCaptionUpdate must be called with permWords included
        assert re.search(r'permWords.*_snCaptionUpdate|_snCaptionUpdate.*permWords', src, re.DOTALL) or \
               re.search(r'permanentPrefix.*_snCaptionUpdate', src, re.DOTALL), (
            "STREAMING-RESET regression: permanent prefix words are not passed to "
            "_snCaptionUpdate. The caption will lose committed text every 30 seconds, "
            "making long recordings appear to drop or reset mid-stream."
        )


# ---------------------------------------------------------------------------
# Regression 9 — WINDOW-HEADER
# A MediaRecorder stream is one continuous container: only chunk 0 carries the
# WebM/EBML (or MP4) header. pumpPartial's windowed slice (chunks.slice(
# partialStart) with partialStart > 0) produced a HEADERLESS blob that ffmpeg
# rejected with "Invalid data found when processing input" — so every partial
# past ~30s of dictation failed server-side. The live caption froze, the
# gap/stability end-of-speech signals died, and the fallback transcript
# (_lastFullText) was capped at the first window: long mobile dictations
# arrived truncated at ~30 seconds. Fixed by prepending chunk 0 to windowed
# partials (the decoder resyncs onto later clusters — verified empirically
# against faster-whisper's decoder), and by advancing the window start in
# whole-PARTIAL_WINDOW hops so LocalAgreement keeps a stable baseline between
# hops and promoted segments are never re-covered (no duplicated caption text).
# ---------------------------------------------------------------------------

class TestWindowHeaderChunkGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_windowed_partial_prepends_header_chunk(self):
        """Windowed partial blobs must prepend chunks[0] (the container header).
        Without it, every partial past ~30s is undecodable server-side and long
        dictations silently truncate at the first window."""
        src = self._src()
        assert re.search(r'\[chunks\[0\]\]\.concat\(chunks\.slice\(partialStart\)\)', src), (
            "WINDOW-HEADER regression: pumpPartial no longer prepends chunks[0] to "
            "the windowed slice. A mid-stream slice has no WebM/MP4 header, ffmpeg "
            "fails with 'Invalid data found when processing input', and every "
            "partial past ~30s of dictation breaks — capping long voice input."
        )

    def test_header_prepend_is_conditional_on_window_shift(self):
        """The chunk-0 prepend must apply ONLY when partialStart > 0. An
        unconditional prepend would duplicate chunk 0's audio in every
        pre-window partial (the common short-dictation case)."""
        src = self._src()
        assert re.search(r'partialStart\s*>\s*0\s*\?\s*\[chunks\[0\]\]', src), (
            "WINDOW-HEADER regression: the chunk-0 prepend must be gated on "
            "partialStart > 0 so un-windowed partials (recordings under ~30s) "
            "keep sending chunks.slice(partialStart) verbatim."
        )

    def test_partial_start_advances_in_whole_window_hops(self):
        """partialStart must advance in whole-PARTIAL_WINDOW hops (segmented,
        non-overlapping windows), not slide continuously. A continuous slide
        advances on every pump past 30s, which (a) trips the LocalAgreement
        reset each cycle so committed words never grow again, and (b) overlaps
        the promoted permanent prefix, duplicating text in the caption and the
        watchdog fallback."""
        src = self._src()
        assert re.search(r'Math\.floor\(.*PARTIAL_WINDOW\)\s*\*\s*PARTIAL_WINDOW', src), (
            "WINDOW-HEADER regression: partialStart no longer advances in "
            "whole-PARTIAL_WINDOW hops. A continuously-sliding window start resets "
            "LocalAgreement on every pump past 30s and re-covers promoted text, "
            "freezing committed words and duplicating the live caption."
        )


# ---------------------------------------------------------------------------
# Regression 10 — BACKSTOP-PREEMPT
# _finish() arms a 7s send backstop to guarantee a send when recorder.onstop
# NEVER fires. But it stayed armed after onstop DID fire, so any final
# transcription slower than 7s (long recording + mobile upload over Tailscale
# + _INFER_LOCK wait) was preempted: the backstop sent the lower-quality live
# transcript and the real full-quality final result was discarded on arrival
# (commitSend is idempotent). Combined with WINDOW-HEADER this hard-truncated
# long mobile dictations at ~30s. Fixed by retiring the backstop at the top of
# recorder.onstop — from there the adaptive chunk-scaled watchdog owns the
# send guarantee.
# ---------------------------------------------------------------------------

class TestFinalizeBackstopGuard:

    def _src(self):
        return _read(_JS / "voice.js")

    def test_onstop_retires_finalize_backstop(self):
        """recorder.onstop must clear controller._sendBackstop near its top.
        Otherwise the 7s backstop races the (up to 60s) adaptive watchdog and
        wins whenever the final transcription takes >7s, replacing the real
        final transcript with the live fallback."""
        src = self._src()
        onstop_pos = src.find("recorder.onstop = ")
        assert onstop_pos != -1, "recorder.onstop assignment not found in voice.js"
        snippet = src[onstop_pos:onstop_pos + 900]
        assert re.search(r'clearTimeout\(controller\._sendBackstop\)', snippet), (
            "BACKSTOP-PREEMPT regression: recorder.onstop does not retire the "
            "finalize-time _sendBackstop. Any final transcription slower than 7s "
            "(routine for long recordings on mobile) is preempted by the backstop, "
            "which sends the live fallback and discards the full-quality final."
        )

    def test_finish_still_arms_backstop_for_dead_recorder(self):
        """_finish() must still arm the send backstop (the recorder-never-stops
        contingency). Retiring it in onstop is only safe because onstop firing
        proves the recorder worked; if _finish() stops arming it, a dead
        recorder silently drops the message."""
        src = self._src()
        assert re.search(r'_sendBackstop\s*=\s*setTimeout', src), (
            "BACKSTOP-PREEMPT regression: _finish() no longer arms _sendBackstop. "
            "If recorder.onstop never fires (recorder error / already inactive), "
            "nothing sends the message — dictation is silently dropped."
        )
