.DEFAULT_GOAL := run

UV ?= uv
PORT ?= 8000
DATA_DIR ?= data/synthetic
DATA_ARGS ?=
DATA_TARGETS := data-prepare data-submit data-status data-wait data-collect data-retry data-attach

.PHONY: setup run dev test help $(DATA_TARGETS)

setup:
	$(UV) sync --locked

run:
	$(UV) run --locked uvicorn doge.app:app --host 127.0.0.1 --port $(PORT)

dev:
	$(UV) run --locked uvicorn doge.app:app --host 127.0.0.1 --port $(PORT) --reload

test:
	$(UV) run --locked python -m unittest discover -s tests -v

$(DATA_TARGETS): data-%:
	$(UV) run --no-project python scripts/generate_data.py $* --out "$(DATA_DIR)" $(DATA_ARGS)

help:
	@echo "make / make run  : Webアプリを起動（既定ポート8000）"
	@echo "make setup       : 依存関係をインストール"
	@echo "make dev         : コード変更時に自動再起動"
	@echo "make test        : 外部APIを使わずテスト"
	@echo "make run PORT=8001 : ポートを変更して起動"
	@echo "make data-prepare : データ生成計画を準備（課金なし）"
	@echo "make data-submit  : バッチ投入（課金あり）"
	@echo "make data-status  : バッチの状態を確認"
	@echo "make data-wait    : 完了を待ってJSONLを保存"
	@echo "make data-collect : 現時点の結果を回収"
	@echo "make data-retry   : 失敗した分だけ再投入（課金あり）"
	@echo "make data-attach DATA_ARGS='--batch-id msgbatch_...' : バッチIDを復旧"
	@echo "生成先の変更: DATA_DIR=data/another / 追加オプション: DATA_ARGS='--seed 123'"
