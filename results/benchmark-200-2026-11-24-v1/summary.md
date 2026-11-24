# Evaluation Summary

Run mode: `formal`

| Controller | Episodes | Accuracy | Mean turns | Mean model calls | Mean latency (ms) | p95 latency (ms, nearest rank) | Mean tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| agent | 600 | 86.8% (521/600) | 1.43 | 2.17 | 3439.0 | 6000 | 9762.8 |
| baseline | 200 | 76.0% (152/200) | 1.40 | 1.03 | 2848.8 | 5213 | 4559.6 |

## Agent repeat metrics

- Repeat 1 accuracy: 87.0%.
- Repeat 2 accuracy: 85.5%.
- Repeat 3 accuracy: 88.0%.
- pass@3: 90.5% across 200 tasks.
- pass^3: 83.0% across 200 tasks.
- Flipped task IDs: A07, A19, A20, A21, A23, A24, C13, C15, C24, C28, P01, P08, P22, U09, U14.
