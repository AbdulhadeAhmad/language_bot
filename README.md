# German Learning Telegram Bot

A private-by-default, multi-user German sentence library with Telegram audio storage and spaced-repetition practice. Implementation is tracked in [implementation_plan.md](implementation_plan.md); the original brief is [project_description.md](project_description.md).

Current work: M0 acceptance scenarios are documented; M1 model benchmarks are prepared for the target server. The bot itself is not implemented yet.

- [Acceptance scenarios](docs/acceptance_scenarios.md)
- [CPU benchmark setup and commands](benchmarks/README.md)

Run benchmark-tool tests without downloading models:

```bash
python3 -m unittest discover -s benchmarks/tests -v
```
