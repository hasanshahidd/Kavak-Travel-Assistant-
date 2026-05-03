# =============================================================================
# Kavak Travel Assistant — Make targets
# =============================================================================

.PHONY: help install run ui demo test lint typecheck eval clean

help: ## Show available targets
	@echo "Kavak Travel Assistant — make targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---- Setup ----
install: ## Install Python deps
	pip install -r requirements.txt

# ---- Run ----
run: ## Run rich-formatted CLI chat
	python main.py

ui: ## Run Streamlit UI (http://localhost:8501)
	streamlit run streamlit_app.py

demo: ## Run scripted demo conversation (5 canned turns)
	python main.py --demo

# ---- Quality ----
test: ## Run pytest
	pytest -q

lint: ## Run ruff
	ruff check app tests evals

typecheck: ## Run mypy
	mypy app

# ---- Eval ----
eval: ## Run golden + adversarial eval suites, write results to evals/results/
	python -m evals.run_eval

# ---- Clean ----
clean: ## Remove caches and build artifacts
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache \
	       .faiss_index .traces .eval_cache evals/results/.cache
