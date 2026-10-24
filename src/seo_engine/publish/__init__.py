"""Publishers put a checked page on the site. [publish] target picks one:

  static    write HTML or Markdown files into a folder (publish/static.py)
  shopify   create or update a Shopify page through the Admin REST API (publish/shopify.py)
  git       the site deploys from the reviewed repo, so merging the pull request publishes

Every publisher has `files(page)` (what goes into the review pull request), `publish(page)` and
`rollback(published)`. The engine calls `publish` only when [publish] mode = "live" and every
gate passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Published:
    action: str  # created, updated, unchanged, deployed by merge
    where: str = ""
    undo: dict[str, Any] = field(default_factory=dict)
    binding: dict[str, Any] = field(default_factory=dict)


def build_publisher(cfg: Any, transport: Any = None) -> Any:
    from .shopify import ShopifyPreview, ShopifyPublisher
    from .static import GitDeployPublisher, StaticPublisher

    target = cfg["publish"]["target"]
    s = cfg["static"]
    if target == "shopify":
        if cfg["publish"]["mode"] != "live":
            return ShopifyPreview()
        return ShopifyPublisher(api_version=cfg["shopify"]["api_version"], transport=transport)
    if target == "git":
        return GitDeployPublisher(cfg.path(s["dir"]), s["file"], s["format"])
    return StaticPublisher(cfg.path(s["dir"]), s["file"], s["format"])
