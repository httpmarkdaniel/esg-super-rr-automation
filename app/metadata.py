from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

TOKEN_PATTERN = re.compile(r"[A-Z0-9]+")


def _tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.upper())


def _phrase_bbox(
    words: list[dict[str, Any]],
    phrase: str,
    *,
    top_limit: float,
) -> dict[str, float] | None:
    flattened: list[tuple[str, int]] = []

    for word_index, word in enumerate(words):
        if float(word["top"]) >= top_limit:
            continue

        flattened.extend(
            (token, word_index)
            for token in _tokens(str(word["text"]))
        )

    expected = _tokens(phrase)

    for start in range(len(flattened) - len(expected) + 1):
        candidate = flattened[start : start + len(expected)]

        if [token for token, _ in candidate] != expected:
            continue

        indexes = sorted({word_index for _, word_index in candidate})
        matched = [words[index] for index in indexes]

        if (
            max(float(word["bottom"]) for word in matched)
            - min(float(word["top"]) for word in matched)
            > 14.0
        ):
            continue

        return {
            "x0": min(float(word["x0"]) for word in matched),
            "top": min(float(word["top"]) for word in matched),
            "x1": max(float(word["x1"]) for word in matched),
            "bottom": max(float(word["bottom"]) for word in matched),
        }

    return None


def _render_words(words: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(
        words,
        key=lambda word: (
            float(word["top"]),
            float(word["x0"]),
        ),
    )

    lines: list[list[dict[str, Any]]] = []
    line_tops: list[float] = []

    for word in ordered:
        top = float(word["top"])

        if not lines or abs(top - line_tops[-1]) > 2.5:
            lines.append([word])
            line_tops.append(top)
        else:
            lines[-1].append(word)

    return " ".join(
        " ".join(
            str(word["text"]).strip()
            for word in line
            if str(word["text"]).strip()
        )
        for line in lines
    ).strip()


def _words_for_value(
    words: list[dict[str, Any]],
    label: dict[str, float],
    *,
    x_end: float,
    continuation_x0: float,
    continuation_depth: float,
) -> list[dict[str, Any]]:
    label_center = (label["top"] + label["bottom"]) / 2.0

    selected = [
        word
        for word in words
        if float(word["x0"]) >= label["x1"] + 2.0
        and float(word["x1"]) <= x_end
        and abs(
            (
                (float(word["top"]) + float(word["bottom"])) / 2.0
            )
            - label_center
        )
        <= 3.0
    ]

    selected.extend(
        word
        for word in words
        if float(word["x0"]) >= continuation_x0
        and float(word["x1"]) <= x_end
        and float(word["top"]) > label["bottom"] + 0.5
        and float(word["top"])
        <= label["top"] + continuation_depth
    )

    unique: dict[tuple[float, float, str], dict[str, Any]] = {}

    for word in selected:
        key = (
            float(word["x0"]),
            float(word["top"]),
            str(word["text"]),
        )
        unique[key] = word

    return list(unique.values())


def extract_account_metadata(path: Path) -> dict[str, str]:
    with pdfplumber.open(path) as document:
        if not document.pages:
            return {
                "account_name": "",
                "billing_address": "",
            }

        page = document.pages[0]

        words = page.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
        )

        top_limit = float(page.height) * 0.42

        account = _phrase_bbox(
            words,
            "ACCOUNT NAME",
            top_limit=top_limit,
        )

        billing = _phrase_bbox(
            words,
            "BILLING ADDRESS",
            top_limit=top_limit,
        )

        pickup = _phrase_bbox(
            words,
            "PICKUP ADDRESS",
            top_limit=top_limit,
        )

        account_name = ""
        billing_address = ""

        if account:
            account_name = _render_words(
                _words_for_value(
                    words,
                    account,
                    x_end=float(page.width) * 0.72,
                    continuation_x0=account["x1"] + 2.0,
                    continuation_depth=0.0,
                )
            )

        if billing:
            billing_end = (
                pickup["x0"] - 2.0
                if pickup
                else float(page.width) * 0.62
            )

            billing_address = _render_words(
                _words_for_value(
                    words,
                    billing,
                    x_end=billing_end,
                    continuation_x0=billing["x1"] + 2.0,
                    continuation_depth=34.0,
                )
            )

        return {
            "account_name": account_name,
            "billing_address": billing_address,
        }