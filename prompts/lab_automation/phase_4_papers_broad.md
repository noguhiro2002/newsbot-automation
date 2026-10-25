# Phase 4 Broad Lane: 論文・preprint独立広域探索

指定期間 `{{PERIOD}}` に公開された、Lab Automationまたは科学実験workflowへ直接接続する論文・preprintを、制約の少ない自然文検索で独立に探してください。

## Independence Rule

このセッションには、論文API候補、API coverage、APIレーンの検索語・候補・除外結果、Phase 1〜3の候補、watchlistを一切渡していません。それらを推測して再現せず、異なる表現、新しい研究領域、新規の公開先を拾う検索を最低3件実行してください。

Audience/editorial feedback is a ranking aid, never a hard exclusion:
{{PREFERENCE_PROFILE}}

## Scope

次のような、科学実験の計画・実行・観測・解析・次条件選択・監査と具体的につながる研究を対象にします。

* self-driving laboratory、closed-loop experimentation、robot scientist
* robotic chemistry、automated synthesis、high-throughput experimentation
* 実験装置、測定装置、液体ハンドラー、培養・合成・分析系の自動化と連携
* machine-readable protocol、laboratory orchestration、実験データ基盤
* ChemWorld型のprogrammable experimental environment
* 科学実験・研究装置・研究工程のDigital Twin / virtual laboratory
* AI Scientist / Autonomous Science Agentの訓練・評価・安全性検証環境
* action、observation、state transition、resource、failure、trajectory、replay、auditを具体的に扱う基盤

物理ロボット接続は必須ではありません。ただし、科学実験との具体的接続がない一般的なLLMベンチマーク、チャットAgent評価、コード生成ベンチマーク、純粋な画像解析・シミュレーションは除外してください。

## Search and Verification Rules

* 固定キーワード一覧や特定出版社だけに依存せず、少なくとも3つの異なる自然文検索から開始する。
* journal、conference proceedings、preprint、project publication page、dataset/method paperを横断する。
* 発見後はcanonical論文URLまたはDOI、投稿日・公開日、abstractまたは本文、科学実験workflowとの直接性を確認する。
* 同一研究のpreprint、改訂版、査読済み版は1候補へ統合し、最も適切なcanonical URLを使う。
* 指定期間外、日付不明、URLを検証できない候補は含めない。
* 件数合わせで低関連候補を入れない。一方で、該当する候補数に固定上限は設けず、確認できた高関連候補をすべて返す。
* API候補は渡されていないため、すべてのcandidateで `paper_api_candidate_ids` を空配列にする。

## Output Contract

返答は単一のJSON objectだけにしてください。Markdown、コードフェンス、説明文は不要です。`confidence`は0.0〜1.0の数値です。

```json
{
  "phase": "{{PHASE_NAME}}",
  "period": "{{PERIOD}}",
  "candidates": [
    {
      "title": "...",
      "source": "...",
      "url": "https://...",
      "published_date": "YYYY-MM-DD",
      "source_type": "paper|preprint|proceedings|dataset",
      "geography": "Japan / US / EU / Global / ...",
      "lab_automation_relevance": "科学実験workflowとの直接的接続",
      "evidence": "canonical URL、日付、abstract・本文で確認した根拠",
      "confidence": 0.0,
      "canonical_source_checked": true,
      "duplicate_key": "normalized-research-title",
      "paper_api_candidate_ids": [],
      "discovery_mode": "broad",
      "discovery_modes": ["broad"],
      "content_kind": "news",
      "event_date_start": "",
      "event_date_end": "",
      "date_basis": "publication",
      "original_publication_date": ""
    }
  ],
  "search_coverage": {
    "queries_run": ["..."],
    "broad_queries_run": ["...", "...", "..."],
    "broad_candidate_count": 0,
    "merged_candidate_count": 0,
    "notable_zero_result_queries": ["..."],
    "limitations": "..."
  }
}
```

`discovery_mode`は常に`"broad"`、`discovery_modes`は`["broad"]`です。`broad_candidate_count`は重複排除前、`merged_candidate_count`は同一研究を統合した後の返却候補数です。
