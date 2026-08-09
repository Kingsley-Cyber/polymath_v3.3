#!/bin/zsh
cd /Users/king/polymath_v3.3/backend/scripts
R=/Users/king/polymath_v3.3/POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/work/remediation
/Users/king/polymath_v3.3/local_ghost_b/.venv/bin/python run_graphify_stress_answer_key.py --input "/Users/king/Downloads/s/CPCS_MX_Hierarchical_Motion_Grammar_Research_Paper.md" --answer-key "$R/sealed_v6_placeholder_key.json" --extract-only --namespace sealed_qualification_v7 --output-dir "$R/sealed_v7" > "$R/sealed_v7.log" 2>&1
echo "v7 exit=$?" >> "$R/v7v8_status.txt"
/Users/king/polymath_v3.3/local_ghost_b/.venv/bin/python run_graphify_stress_answer_key.py --input "/Users/king/Downloads/s/CPCS_FACS_Laban_AI_Video_Directorial_Control_Research_Paper.md" --answer-key "$R/sealed_v6_placeholder_key.json" --extract-only --namespace sealed_qualification_v8 --output-dir "$R/sealed_v8" > "$R/sealed_v8.log" 2>&1
echo "v8 exit=$?" >> "$R/v7v8_status.txt"
