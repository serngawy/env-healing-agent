"""
Gemini Diagnostic Client
========================

Sends a chunk of log lines to the Gemini API and returns:
  - A structured diagnosis for the detected issue
  - Any new issue patterns Gemini identifies that are absent from known_issues.json

Authentication: API key via GEMINI_API_KEY environment variable.
Required environment variables:
  GEMINI_API_KEY  — Gemini API key
  GEMINI_MODEL    — model name (default: gemini-2.0-flash)
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

# Deferred import — the caller checks for ImportError so the rest of the
# package still loads even if the `google-generativeai` package is not installed.
import google.generativeai as genai

# Shared prompt — identical to the Claude client so both clients are
# interchangeable from the diagnostic agent's perspective.
_SYSTEM_PROMPT = """\
You are an expert SRE diagnostic agent specialising in OpenShift, Kubernetes, \
and ROSA (Red Hat OpenShift Service on AWS) infrastructure.

You will receive a JSON object with:
  - issue_type        : the pattern type that was matched in the log stream
  - log_chunk         : error/failure log lines with 10 lines of context before
                        and after each one; windows are separated by "--- window N ---"
                        markers. If no error lines were found the full tail is sent.
  - existing_patterns : types + descriptions of patterns already in known_issues.json
  - available_fix_strategies : valid keys from fix_strategies.json

Your tasks:
1. Diagnose the root cause of the detected issue using the log evidence.
2. Select the best fix strategy from the provided list.
3. Identify any NEW issue patterns visible in the log chunk that are NOT already
   covered by the existing patterns.

Respond ONLY with valid JSON — no markdown, no prose — in exactly this structure:

{
  "diagnosis": {
    "issue_type": "<the issue_type provided>",
    "root_cause": "<specific root cause — 1-2 sentences>",
    "severity": "low|medium|high|critical",
    "confidence": <float 0.0-1.0>,
    "evidence": ["<specific log line or observation>", ...],
    "recommended_fix": "<one of the available_fix_strategies keys>",
    "fix_parameters": {}
  },
  "new_patterns": [
    {
      "type": "<unique_snake_case_identifier>",
      "pattern": "<valid Python regex, case-insensitive>",
      "severity": "low|medium|high|critical",
      "auto_fix": false,
      "description": "<what this issue is>",
      "symptoms": ["<observable symptom>"],
      "common_causes": ["<likely root cause>"]
    }
  ]
}

Rules:
- recommended_fix MUST be one of the provided available_fix_strategies keys.
  Use "log_and_continue" if none fit.
- new_patterns MUST be [] when all visible issues are already covered by
  existing_patterns or when no distinct new issue is visible.
- confidence reflects certainty about the root cause given the log evidence
  (0.0 = guessing, 1.0 = definitive from explicit log evidence).
- evidence entries must quote or paraphrase actual lines from the log_chunk.
"""

_CONTEXT_LINES = 10
_ERROR_RE = re.compile(r"\b(error|fail(?:ed|ing)?|fatal|exception|traceback)\b", re.IGNORECASE)
_FALLBACK_LINES = 30


def _extract_error_windows(lines: List[str], context: int = _CONTEXT_LINES) -> str:
    if not lines:
        return "(no log lines available)"

    n = len(lines)
    error_indices = [i for i, line in enumerate(lines) if _ERROR_RE.search(line)]

    if not error_indices:
        return "\n".join(lines[-_FALLBACK_LINES:])

    ranges: List[tuple] = []
    for idx in error_indices:
        start = max(0, idx - context)
        end = min(n - 1, idx + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    sections: List[str] = []
    for window_num, (start, end) in enumerate(ranges, start=1):
        header = f"--- window {window_num} (lines {start + 1}–{end + 1}) ---"
        body = "\n".join(lines[start : end + 1])
        sections.append(f"{header}\n{body}")

    return "\n\n".join(sections)


class GeminiClient:
    """Thin wrapper around the Gemini generative AI SDK for diagnostic use."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        resolved_key = api_key or os.environ["GEMINI_API_KEY"]
        genai.configure(api_key=resolved_key)
        self._model_name = model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self._model = genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=_SYSTEM_PROMPT,
        )

    def diagnose(
        self,
        issue_type: str,
        log_chunk: List[str],
        known_patterns: List[Dict],
        fix_strategies: Dict,
    ) -> Tuple[Optional[Dict], List[Dict]]:
        """
        Ask Gemini to diagnose an issue from a log chunk.

        Same interface as ClaudeClient.diagnose() so both clients are
        interchangeable within DiagnosticAgent.

        Returns
        -------
        diagnosis
            Structured dict ready for the remediation agent, or None on failure.
        new_patterns
            New issue patterns Gemini identified; empty list when none found.
        """
        log_text = _extract_error_windows(log_chunk)

        existing_summary = [
            {
                "type": p.get("type"),
                "description": (p.get("description") or "")[:100],
            }
            for p in known_patterns
        ]

        fix_summary = {
            key: strat.get("description", "")
            for key, strat in fix_strategies.items()
        }

        payload = {
            "issue_type": issue_type,
            "log_chunk": log_text,
            "existing_patterns": existing_summary,
            "available_fix_strategies": fix_summary,
        }

        response = self._model.generate_content(json.dumps(payload, indent=2))
        raw = response.text.strip()

        # Strip markdown code fences that Gemini sometimes wraps around JSON.
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        data = json.loads(raw)
        diagnosis = data.get("diagnosis")
        new_patterns = data.get("new_patterns") or []
        return diagnosis, new_patterns
