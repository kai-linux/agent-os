# Cost Tracking

`orchestrator/cost_tracker.py` rebuilds `runtime/metrics/cost_records.jsonl` from `runtime/metrics/agent_stats.jsonl`.

## What It Tracks

- One `task_cost` record per task, with per-attempt estimated cost breakdowns
- One `repo_summary` record per repo
- One `global_summary` record for the full metrics file

The queue refreshes `cost_records.jsonl` after each successful `agent_stats.jsonl` write, and you can also run the tracker directly:

```bash
python -m orchestrator.cost_tracker
```

## Pricing Assumptions

The baseline price table is hard-coded near the top of `orchestrator/cost_tracker.py`.

Current built-in model families:

- `claude-sonnet-4` via Anthropic: `$3.00` input, `$15.00` output per 1M tokens
- `claude-opus-4` via Anthropic: `$15.00` input, `$75.00` output per 1M tokens
- `gemini-2.5-flash` via Google: `$0.30` input, `$2.50` output per 1M tokens
- `deepseek/deepseek-v3.2` via DeepSeek-family baseline: `$0.27` input, `$1.10` output per 1M tokens
- `codex` via OpenAI: `$15.00` input, `$60.00` output per 1M tokens

These are governance-grade approximations, not billing-grade invoices.

## Limitations

- Token counts are estimated as `characters / 4`, rounded up
- Input tokens are estimated from the final prompt snapshot passed to the runner
- Output tokens are estimated from `.agent_result.md`, not raw provider transcripts
- DeepSeek may run through `openrouter`, `nanogpt`, or `chutes`; current tracking uses one shared DeepSeek-family baseline unless you override it
- Historical `agent_stats.jsonl` rows written before this feature have no `model_attempt_details`, so they rebuild as zero-cost task records

## Updating Prices

Update either of these places:

1. Change the built-in `PRICING_CATALOG` constant for the new baseline.
2. Adjust your local `config.yaml` with a `cost_tracking` section.

Example:

```yaml
cost_tracking:
  default_price_multiplier: 1.0
  provider_multipliers:
    openai: 1.1
  model_overrides:
    codex:
      input_per_million_tokens: 12.0
      output_per_million_tokens: 48.0
```

Use `agent_models` in `config.yaml` when your runtime is pinned to a different default model family than the built-in mapping.
