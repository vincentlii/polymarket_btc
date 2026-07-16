"""In-memory catalog for validated BTC Up/Down market metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from btc_short_horizon.data.contracts import (
    BTC_15M_MARKET_FAMILY,
    BTC_5M_MARKET_FAMILY,
    BtcMarketFamily,
    MarketValidationError,
    MarketWindow,
)


@dataclass(frozen=True)
class ParsedMarketSlug:
    """The family and UTC window start encoded by a canonical market slug."""

    family: BtcMarketFamily
    t0: datetime


class MarketCatalog:
    """Registers provided market metadata without performing network discovery."""

    def __init__(
        self,
        *,
        families: Iterable[BtcMarketFamily] = (
            BTC_15M_MARKET_FAMILY,
            BTC_5M_MARKET_FAMILY,
        ),
        windows: Iterable[MarketWindow] = (),
    ) -> None:
        configured_families = tuple(families)
        if not configured_families:
            raise MarketValidationError("MarketCatalog requires at least one market family.")

        self._families_by_name: dict[str, BtcMarketFamily] = {}
        slug_identities: set[tuple[str, int]] = set()
        for family in configured_families:
            if not isinstance(family, BtcMarketFamily):
                raise MarketValidationError("families must contain only BtcMarketFamily values.")
            if family.name in self._families_by_name:
                raise MarketValidationError(f"Duplicate market family name {family.name!r}.")
            slug_identity = (family.slug_prefix, family.window_seconds)
            if slug_identity in slug_identities:
                raise MarketValidationError(
                    "Market families with the same slug prefix and window are ambiguous."
                )
            self._families_by_name[family.name] = family
            slug_identities.add(slug_identity)

        self._families = configured_families
        self._windows_by_slug: dict[str, MarketWindow] = {}
        for window in windows:
            self.register(window)

    @property
    def families(self) -> tuple[BtcMarketFamily, ...]:
        """Return the configured market families in registration order."""
        return self._families

    def parse_slug(self, slug: str) -> ParsedMarketSlug:
        """Resolve a canonical slug to exactly one configured market family."""
        matches: list[ParsedMarketSlug] = []
        for family in self._families:
            try:
                t0 = family.parse_slug(slug)
            except MarketValidationError:
                continue
            matches.append(ParsedMarketSlug(family=family, t0=t0))

        if len(matches) != 1:
            raise MarketValidationError(f"slug {slug!r} does not resolve to one configured family.")
        return matches[0]

    def register(self, window: MarketWindow) -> None:
        """Add one validated market window, rejecting duplicate or unknown families."""
        if not isinstance(window, MarketWindow):
            raise MarketValidationError("window must be a MarketWindow.")
        configured_family = self._families_by_name.get(window.family.name)
        if configured_family is None:
            raise MarketValidationError(f"Unknown market family {window.family.name!r}.")
        if configured_family != window.family:
            raise MarketValidationError(
                f"Market family {window.family.name!r} does not match the catalog configuration."
            )

        parsed = self.parse_slug(window.slug)
        if parsed.family != configured_family or parsed.t0 != window.t0:
            raise MarketValidationError("window slug does not match its configured market family.")
        if window.slug in self._windows_by_slug:
            raise MarketValidationError(f"Duplicate market slug {window.slug!r}.")
        self._windows_by_slug[window.slug] = window

    def get(self, slug: str) -> MarketWindow | None:
        """Return a registered window by canonical slug, if present."""
        return self._windows_by_slug.get(slug)

    def require(self, slug: str) -> MarketWindow:
        """Return a registered window or raise ``KeyError`` when it is absent."""
        window = self.get(slug)
        if window is None:
            raise KeyError(slug)
        return window

    def windows(self, *, family_name: str | None = None) -> tuple[MarketWindow, ...]:
        """Return registered windows ordered by start time and slug."""
        if family_name is None:
            selected = self._windows_by_slug.values()
        else:
            family = self._families_by_name.get(family_name.strip().casefold())
            if family is None:
                raise KeyError(family_name)
            selected = (
                window for window in self._windows_by_slug.values() if window.family == family
            )
        return tuple(sorted(selected, key=lambda window: (window.t0, window.slug)))

    def __len__(self) -> int:
        return len(self._windows_by_slug)


__all__ = ["MarketCatalog", "ParsedMarketSlug"]
