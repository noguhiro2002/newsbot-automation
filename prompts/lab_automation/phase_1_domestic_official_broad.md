# Phase 1 Broad Lane: 国内公式・研究基盤

指定期間 `{{PERIOD}}` の日本国内におけるLab Automationまたは科学実験workflowに直接関係する重要な公式ニュースを、制約の少ない自然文検索で独立に探してください。

このセッションにはstructuredレーンの検索語、watchlist、候補、feed候補を一切渡していません。組織名や固定キーワード一覧を前提にせず、異なる表現や新規主体を拾う検索を最低3件実行してください。Phase境界は国内公式・公的研究基盤に限定し、論文・preprintとPR配信主体の探索は除外します。公式URL、canonical公開日、科学実験との具体的接続を確認してください。

Audience/editorial feedback is a ranking aid, never a hard exclusion:
{{PREFERENCE_PROFILE}}

イベントは重要な開催自体なら候補にできますが、単なる出展・一般セミナーは除外してください。イベント日を公開日に代入しないでください。

JSONだけを返してください。`discovery_mode`と`discovery_modes`はトップレベルではなく、各candidateの中へ必ず入れてください。`confidence`は`high`などの文字列ではなく、0.0〜1.0の数値にしてください。

```json
{
  "phase": "{{PHASE_NAME}}",
  "period": "{{PERIOD}}",
  "candidates": [{
    "title": "...", "source": "...", "url": "https://...",
    "published_date": "YYYY-MM-DD", "source_type": "official", "geography": "Japan",
    "lab_automation_relevance": "...", "evidence": "...", "confidence": 0.85,
    "canonical_source_checked": true, "discovery_mode": "broad",
    "discovery_modes": ["broad"], "duplicate_key": "...",
    "content_kind": "news", "event_date_start": "", "event_date_end": "",
    "date_basis": "publication", "original_publication_date": ""
  }],
  "search_coverage": {
    "broad_queries_run": ["...", "...", "..."],
    "broad_candidate_count": 1, "merged_candidate_count": 1, "limitations": "..."
  }
}
```
