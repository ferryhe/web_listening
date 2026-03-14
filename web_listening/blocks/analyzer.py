from collections import Counter
from datetime import datetime, timezone
from typing import List

from web_listening.config import settings
from web_listening.models import AnalysisReport, Change


class Analyzer:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
        return self._client

    def analyze_changes(
        self,
        changes: List[Change],
        period_start: datetime,
        period_end: datetime,
    ) -> AnalysisReport:
        if not changes:
            summary = "No changes detected during this period."
        else:
            summary = self._generate_summary(changes)

        site_ids = list({c.site_id for c in changes})
        return AnalysisReport(
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(timezone.utc),
            site_ids=site_ids,
            summary_md=summary,
            change_count=len(changes),
        )

    def _generate_summary(self, changes: List[Change]) -> str:
        if not settings.openai_api_key:
            return self._local_summary(changes)

        changes_text = "\n".join(
            f"- [{c.change_type}] Site {c.site_id} at {c.detected_at}: {c.summary}"
            for c in changes[:100]
        )
        prompt = (
            "You are a research analyst. Below are website monitoring changes detected over the past week.\n"
            "Please write a concise markdown summary of the key changes, grouped by type and significance.\n\n"
            f"Changes:\n{changes_text}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            return self._local_summary(changes) + f"\n\n[AI analysis unavailable: {e}]"

    def _local_summary(self, changes: List[Change]) -> str:
        type_counts = Counter(c.change_type for c in changes)
        lines = ["## Weekly Change Summary", "", f"Total changes: {len(changes)}", ""]
        for change_type, count in type_counts.items():
            lines.append(f"- **{change_type}**: {count} changes")
        lines.append("")
        lines.append("### Recent Changes")
        for c in changes[-10:]:
            lines.append(f"- [{c.change_type}] {c.summary} (site {c.site_id})")
        return "\n".join(lines)
