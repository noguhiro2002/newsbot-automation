# Phase 3 Broad Lane: 海外公式

指定期間 `{{PERIOD}}` の海外企業・大学・研究機関・標準化団体による、Lab Automationまたは科学実験workflowに直接関係する重要な公式ニュースを独立した自然文検索で探してください。

structuredレーンの検索語、watchlist、候補、feed候補は受け取っていません。固定組織・固定語に依存しない検索を最低3件実行してください。公式URLとcanonical公開日を確認し、論文・preprintとPR配信主体中心の候補は別Phaseへ譲ってください。

Audience/editorial feedback is a ranking aid, never a hard exclusion:
{{PREFERENCE_PROFILE}}

重要なイベント開催自体は候補にできますが、イベント日を公開日に代入せず、単なる出展・一般セミナーは除外してください。

JSONだけを返してください。`discovery_mode: "broad"`と`discovery_modes: ["broad"]`はトップレベルではなく各candidate内へ必ず入れ、`confidence`は0.0〜1.0の数値にしてください。phaseは`{{PHASE_NAME}}`、periodは`{{PERIOD}}`です。

`search_coverage.broad_queries_run`は検索回数を表す数値ではありません。実行した検索文そのものを格納するJSON文字列配列で、空文字を含めず最低3要素にしてください。`50`や`{"count": 50}`のような形式は禁止です。検索回数を別途示したい場合は`broad_query_count`へ数値で入れてください。

トップレベルの必須形は次の通りです。

```json
{
  "phase": "{{PHASE_NAME}}",
  "period": "{{PERIOD}}",
  "candidates": [],
  "search_coverage": {
    "broad_queries_run": ["実際の検索文1", "実際の検索文2", "実際の検索文3"],
    "broad_query_count": 3,
    "broad_candidate_count": 0,
    "merged_candidate_count": 0,
    "limitations": ""
  }
}
```

各candidateの形式例:

```json
{"title":"...","source":"...","url":"https://...","published_date":"YYYY-MM-DD","source_type":"official","geography":"Global","lab_automation_relevance":"...","evidence":"...","confidence":0.85,"canonical_source_checked":true,"discovery_mode":"broad","discovery_modes":["broad"],"duplicate_key":"...","content_kind":"news","event_date_start":"","event_date_end":"","date_basis":"publication","original_publication_date":""}
```
