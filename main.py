"""ClawTeam Notification Channel Gateway entrypoint."""

from pathlib import Path

from core.config import AppConfig
from core.service import GatewayService
from channels.feishu_ws import FeishuWSAdapter


def main() -> None:
    base_dir = Path.cwd()
    cfg = AppConfig.load(base_dir)
    service = GatewayService(cfg)

    adapters = []
    feishu_cfg = cfg.channels.get("feishu", {})
    if feishu_cfg.get("enabled", True):
        adapters.append(FeishuWSAdapter(service, feishu_cfg, base_dir))

    if not adapters:
        raise RuntimeError("No enabled channel adapters. Check configs/channels.yaml")

    for adapter in adapters:
        adapter.start()

    service.run_forever()


if __name__ == "__main__":
    main()
