"""
Visual Workspace — counter-visual-generate + topic-card-generate skills.

Skills:
  1. generate_clarification_card  — single counter-message card (critic-gated)
  2. generate_topic_cards         — one structured infographic per trending topic
     Layout: SD-generated themed background + Pillow data layer
     Card content:
       - Topic label + risk/trending badges
       - Propagation path: posts in chronological order with username,
         date, action type (INTRODUCED / AMPLIFIED / EXTENDED) and snippet
       - Official sources retrieved from RAG (media name + full title)
       - Verdict section: misinfo risk score + key flags
"""
from __future__ import annotations

import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog
from PIL import Image, ImageDraw

from config import COUNTER_VISUALS_DIR
from models.claim import Claim
from models.post import Post
from models.report import TopicSummary
from services.stable_diffusion_service import StableDiffusionService, _load_font

if TYPE_CHECKING:
    from services.kuzu_service import KuzuService

log = structlog.get_logger(__name__)

# Card dimensions
CARD_W = 1200

# Colour palette
C_BG_OVERLAY   = (10,  15,  30,  215)   # dark navy overlay on SD background
C_SECTION_BG   = (20,  28,  45,  200)   # section panels
C_GOLD         = (255, 215,  50)         # titles / section headers accent
C_BLUE_LIGHT   = (100, 190, 255)         # section label text
C_WHITE        = (240, 240, 240)
C_GREY_LIGHT   = (180, 180, 180)
C_GREY_MED     = (110, 110, 110)
C_SEP          = ( 70,  80, 100)         # separator lines

C_BADGE_MISINFO  = (200,  50,  50)
C_BADGE_TRENDING = (190, 130,   0)
C_BADGE_SAFE     = ( 40, 160,  80)

C_ACTION_INTRODUCED = ( 50, 140, 255)    # blue  – first appearance
C_ACTION_AMPLIFIED  = (170, 140,   0)    # amber – re-broadcast
C_ACTION_EXTENDED   = (220, 110,  30)    # orange – adds new angle

C_SOURCE_BADGE = ( 30,  70, 120)

# Phase 0: Emotion colour map
C_EMOTION: dict[str, tuple] = {
    "fear":    (200,  60,  60),
    "anger":   (220, 100,  20),
    "hope":    ( 40, 180,  80),
    "disgust": (150,  50, 160),
    "neutral": (110, 110, 110),
}


def _safe(s: str, maxlen: int = 999) -> str:
    """
    Convert a string to plain ASCII suitable for Pillow rendering.
    Common Unicode punctuation is mapped to ASCII equivalents first,
    then any remaining non-ASCII bytes are dropped (not replaced with '?').
    """
    _MAP = {
        # Smart quotes
        "\u2018": "'", "\u2019": "'", "\u201a": ",",
        "\u201c": '"', "\u201d": '"', "\u201e": '"',
        # Dashes & hyphens
        "\u2010": "-", "\u2011": "-", "\u2012": "-",
        "\u2013": "-", "\u2014": "--", "\u2015": "--",
        # Ellipsis & bullets
        "\u2026": "...", "\u2022": "*", "\u2023": ">",
        "\u00b7": ".", "\u2219": ".",
        # Arrows
        "\u2192": "->", "\u2190": "<-", "\u2193": "v", "\u2191": "^",
        "\u21d2": "=>",
        # Misc
        "\u00a0": " ", "\u2003": " ", "\u2009": " ",
        "\u00d7": "x", "\u00f7": "/",
    }
    result = s[:maxlen]
    for ch, repl in _MAP.items():
        result = result.replace(ch, repl)
    return result.encode("ascii", "ignore").decode("ascii")


def _wrap(text: str, width: int) -> str:
    return textwrap.fill(text, width=width)


class VisualAgent:
    def __init__(
        self,
        sd: StableDiffusionService,
        kuzu: Optional["KuzuService"] = None,
    ) -> None:
        self._sd = sd
        self._kuzu = kuzu
        self._output_dir = Path(COUNTER_VISUALS_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ── Skill 1: counter-visual-generate ──────────────────────────────────────

    def generate_clarification_card(
        self,
        counter_message: str,
        claim: Claim,
        report_id: str = "",
    ) -> Optional[str]:
        """
        Generate a 1200×675 visual clarification card.
        Returns the local file path on success, or None on failure.
        """
        background_prompt = self._make_background_prompt(claim)
        claim_summary = claim.normalized_text[:120]
        result = self._sd.generate_card(
            counter_text=counter_message,
            background_prompt=background_prompt,
            claim_summary=claim_summary,
            report_id=report_id,
        )
        if result is None:
            log.warning("visual.card_unavailable", claim_id=claim.id)
        else:
            log.info("visual.card_generated", path=result, claim_id=claim.id)
        return result

    # ── Skill 1b: evidence-context-card ───────────────────────────────────────

    def generate_evidence_context_card(
        self,
        claim: Claim,
        report_id: str = "",
    ) -> Optional[str]:
        """
        Render a two-column Evidence/Context card for a non_actionable claim
        that has >=2 supporting-evidence items (§10.4).

        Left column (Supported facts): up to 3 supporting-evidence quotes with
        source badge. Right column (Analyst note): fixed-template paragraph
        making explicit that these facts do NOT endorse the claim's framing.
        """
        supports = claim.supporting_evidence[:3]
        if not supports:
            log.warning(
                "visual.evidence_context_no_support", claim_id=claim.id
            )
            return None

        CARD_W_LOCAL = CARD_W
        CARD_H_LOCAL = 720
        img = Image.new("RGB", (CARD_W_LOCAL, CARD_H_LOCAL), color=(18, 24, 40))
        draw = ImageDraw.Draw(img)

        f_xl = _load_font(38)
        f_lg = _load_font(26)
        f_md = _load_font(20)
        f_sm = _load_font(17)
        f_xs = _load_font(15)

        PAD = 32
        y = 30

        self._badge(draw, PAD, y, "EVIDENCE / CONTEXT", f_md, C_BADGE_TRENDING)
        self._badge(draw, PAD + 280, y, "NON-ACTIONABLE", f_md, (90, 90, 110))
        y += 60

        draw.text((PAD, y), "Claim under review:", fill=C_BLUE_LIGHT, font=f_md)
        y += 30
        claim_title = _safe(claim.normalized_text)
        for line in _wrap(claim_title, 68).splitlines()[:3]:
            draw.text((PAD, y), line, fill=C_GOLD, font=f_lg)
            y += 32
        y += 14
        draw.line(
            [(PAD, y), (CARD_W_LOCAL - PAD, y)], fill=C_SEP, width=2
        )
        y += 16

        col_w = (CARD_W_LOCAL - 3 * PAD) // 2
        col_l_x = PAD
        col_r_x = PAD * 2 + col_w
        col_top = y

        # Left column: Supported facts
        draw.text(
            (col_l_x, col_top), "SUPPORTED FACTS", fill=C_BLUE_LIGHT, font=f_lg
        )
        cy_l = col_top + 38
        for ev in supports:
            src = _safe(ev.source_name or "Source", 24)
            bw = max(len(src) * 11 + 20, 80)
            draw.rectangle(
                [col_l_x, cy_l, col_l_x + bw, cy_l + 26],
                fill=C_SOURCE_BADGE,
            )
            draw.text(
                (col_l_x + 10, cy_l + 4), src,
                fill=(130, 190, 255), font=f_sm,
            )
            cy_l += 34
            snippet = _safe(ev.snippet or ev.article_title or "", 500)
            for line in _wrap(snippet, 46).splitlines()[:5]:
                draw.text((col_l_x, cy_l), line, fill=C_WHITE, font=f_sm)
                cy_l += 22
            cy_l += 12

        # Right column: Analyst note (fixed template per §10.4)
        draw.text(
            (col_r_x, col_top), "ANALYST NOTE", fill=C_BLUE_LIGHT, font=f_lg
        )
        cy_r = col_top + 38
        analyst_note = (
            "These facts are drawn from authoritative sources about the "
            "entities named in the claim. They do NOT confirm the claim's "
            "broader interpretation or framing."
        )
        for line in _wrap(_safe(analyst_note), 44).splitlines():
            draw.text((col_r_x, cy_r), line, fill=C_GREY_LIGHT, font=f_md)
            cy_r += 26
        cy_r += 18

        reason = claim.non_actionable_reason or "insufficient_evidence"
        reason_copy = {
            "context_sparse": (
                "Reason: retrieval surface was too narrow to produce a "
                "direct rebuttal."
            ),
            "insufficient_evidence": (
                "Reason: the classifier did not find directly contradicting "
                "evidence; readers should treat the claim as unverified."
            ),
            "non_factual_expression": (
                "Reason: the claim is phrased as interpretation rather than "
                "a falsifiable factual statement."
            ),
        }.get(reason, f"Reason: {reason}")
        for line in _wrap(_safe(reason_copy), 44).splitlines():
            draw.text((col_r_x, cy_r), line, fill=C_GREY_MED, font=f_sm)
            cy_r += 22

        # Footer
        y = CARD_H_LOCAL - 50
        draw.line([(PAD, y), (CARD_W_LOCAL - PAD, y)], fill=C_SEP, width=1)
        draw.text(
            (PAD, y + 10),
            "Society Analysis System  |  Evidence / Context card  |  "
            "Not an endorsement",
            fill=C_GREY_MED,
            font=f_xs,
        )

        ts_str = int(time.time())
        filename = f"evidence_context_{claim.id[:12]}_{ts_str}.png"
        out_path = self._output_dir / filename
        img.save(str(out_path), "PNG")
        log.info("visual.evidence_context_generated",
                 path=str(out_path), claim_id=claim.id)
        return str(out_path)

    # ── Skill 2: topic-card-generate ──────────────────────────────────────────

    def generate_topic_cards(
        self,
        topic_summaries: list[TopicSummary],
        topics: list[dict],
        all_posts: list[Post],
        report_id: str = "",
    ) -> list[str]:
        """
        Generate one visual infographic card per trending topic.

        Each card contains:
          - Header: topic label, risk badge, trending badge, velocity stats
          - Propagation path: posts sorted by time with username, date,
            action type (INTRODUCED / AMPLIFIED / EXTENDED), text snippet
          - Official sources from RAG: media name + full article title
          - Verdict: misinfo risk score + representative claims

        Returns list of saved PNG file paths.
        """
        post_by_id: dict[str, Post] = {p.id: p for p in all_posts}
        # topic_id → topic dict (with claims)
        topic_by_id: dict[str, dict] = {t["topic_id"]: t for t in topics}

        paths: list[str] = []
        for ts in topic_summaries:
            if not ts.is_trending:
                continue
            topic = topic_by_id.get(ts.topic_id)
            if topic is None:
                continue
            try:
                path = self._render_topic_card(
                    ts, topic, post_by_id, report_id
                )
                if path:
                    paths.append(path)
                    log.info("visual.topic_card_saved",
                             topic=_safe(ts.label[:50]), path=path)
            except Exception as exc:
                log.error("visual.topic_card_error",
                          topic=_safe(ts.label[:50]), error=str(exc))
        return paths

    # ── Internal ───────────────────────────────────────────────────────────────

    def _render_topic_card(
        self,
        ts: TopicSummary,
        topic: dict,
        post_by_id: dict[str, Post],
        report_id: str,
    ) -> Optional[str]:
        """Render one topic card; return saved path or None."""

        # ── Gather data ────────────────────────────────────────────────────
        claims = topic.get("claims", [])

        # Build post_id → [claim_texts] for INTRODUCED / EXTENDED detection
        introduced_by: dict[str, list[str]] = {}
        for c in claims:
            if c.first_seen_post:
                introduced_by.setdefault(c.first_seen_post, []).append(
                    c.normalized_text[:60]
                )

        # Get posts in this topic (via kuzu BelongsToTopic or claim→post)
        topic_post_ids: set[str] = set()
        if self._kuzu:
            for row in self._kuzu.get_topic_posts(ts.topic_id):
                topic_post_ids.add(row["post_id"])
        if not topic_post_ids:
            # Fallback: gather from claim.first_seen_post
            for c in claims:
                if c.first_seen_post:
                    topic_post_ids.add(c.first_seen_post)

        topic_posts = sorted(
            [post_by_id[pid] for pid in topic_post_ids if pid in post_by_id],
            key=lambda p: p.posted_at or datetime.min.replace(tzinfo=timezone.utc),
        )

        # Collect unique evidence sources (title + media name + url)
        seen_src: set[str] = set()
        evidence_sources: list[dict] = []
        for c in claims:
            for ev_list in (c.supporting_evidence,
                            c.contradicting_evidence,
                            c.uncertain_evidence):
                for ev in ev_list:
                    if ev.article_title and ev.article_id not in seen_src:
                        seen_src.add(ev.article_id)
                        evidence_sources.append({
                            "source_name": ev.source_name or "",
                            "title": ev.article_title,
                            "url": ev.article_url or "",
                            "stance": ev.stance,
                        })

        # ── Layout metrics ─────────────────────────────────────────────────
        NUM_POSTS    = min(len(topic_posts),  6)
        NUM_SOURCES  = min(len(evidence_sources), 6)
        TITLE_LINES  = max(1, (len(ts.label) // 52) + 1)
        # Phase 0: Mutation timeline entries
        mutation_chain = (
            self._kuzu.get_claim_mutation_chain(ts.topic_id)
            if self._kuzu else []
        )
        NUM_MUTATION = min(len(mutation_chain), 5)

        HEADER_H     = 30 + 50 + 15 + TITLE_LINES * 56 + 12 + 2 + 12
        # Phase 0: emotion bar (40px) when distribution present
        EMOTION_H    = 50 if ts.emotion_distribution else 0
        PATH_H       = 40 + NUM_POSTS * 82 + max(0, NUM_POSTS - 1) * 24 + 20
        SEP_H        = 2 + 12
        SOURCES_H    = 40 + max(NUM_SOURCES, 1) * 38 + 10
        VERDICT_H    = 2 + 12 + 36 + max(len(ts.representative_claims[:2]), 1) * 32 + 10
        # Phase 0: mutation timeline section (only when chain has ≥ 2 entries)
        MUTATION_H   = (2 + 12 + 32 + NUM_MUTATION * 26 + 10) if NUM_MUTATION >= 2 else 0
        FOOTER_H     = 2 + 42

        CARD_H = (HEADER_H + EMOTION_H + PATH_H + SEP_H
                  + SOURCES_H + VERDICT_H + MUTATION_H + FOOTER_H + 20)

        # ── Background ─────────────────────────────────────────────────────
        bg_prompt = self._topic_bg_prompt(ts)
        bg = self._sd.generate_background_only(bg_prompt)
        bg = bg.resize((CARD_W, CARD_H), Image.LANCZOS)

        # Dark overlay
        overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(
            [0, 0, CARD_W, CARD_H], fill=C_BG_OVERLAY
        )
        img = Image.alpha_composite(
            bg.convert("RGBA"), overlay
        ).convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── Fonts ──────────────────────────────────────────────────────────
        f_xl  = _load_font(46)
        f_lg  = _load_font(30)
        f_md  = _load_font(22)
        f_sm  = _load_font(18)
        f_xs  = _load_font(15)

        # ── Drawing cursor ─────────────────────────────────────────────────
        y = 20
        PAD = 28

        # ── HEADER BADGES ──────────────────────────────────────────────────
        # Risk badge
        risk_color = C_BADGE_MISINFO if ts.is_likely_misinfo else (
            C_BADGE_TRENDING if ts.misinfo_risk >= 0.4 else C_BADGE_SAFE
        )
        risk_label = (
            f"MISINFO  {ts.misinfo_risk:.0%}" if ts.is_likely_misinfo
            else f"RISK  {ts.misinfo_risk:.0%}"
        )
        self._badge(draw, PAD, y, risk_label, f_md, risk_color)

        # Trending badge
        if ts.is_trending:
            self._badge(draw, PAD + 200, y, "TRENDING", f_md, C_BADGE_TRENDING)

        # Stats right-side
        stats = _safe(
            f"{ts.post_count} posts  \u2022  {ts.velocity:.1f} posts/hr"
            f"  \u2022  {ts.claim_count} claims"
        )
        draw.text((CARD_W - 420, y + 8), stats, fill=C_GREY_LIGHT, font=f_md)
        y += 60

        # Topic title
        title_text = _safe(ts.label, 80)
        for line in _wrap(title_text, 54).splitlines():
            draw.text((PAD, y), line, fill=C_GOLD, font=f_xl)
            y += 56
        y += 8

        # Separator
        draw.line([(PAD, y), (CARD_W - PAD, y)], fill=C_SEP, width=2)
        y += 14

        # ── EMOTION DISTRIBUTION BAR (Phase 0) ─────────────────────────────
        if ts.emotion_distribution:
            bar_w = CARD_W - 2 * PAD
            bar_h = 18
            # Label
            dom_label = _safe(f"EMOTION  [{ts.dominant_emotion.upper()}]")
            draw.text((PAD, y), dom_label, fill=C_BLUE_LIGHT, font=f_sm)
            y += 22
            # Stacked colour bar
            x_cursor = PAD
            # Sort so dominant is first
            sorted_emotions = sorted(
                ts.emotion_distribution.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
            for emotion, frac in sorted_emotions:
                seg_w = max(int(bar_w * frac), 1)
                color = C_EMOTION.get(emotion, (110, 110, 110))
                draw.rectangle([x_cursor, y, x_cursor + seg_w, y + bar_h], fill=color)
                # Tiny label inside segment if wide enough
                if seg_w > 40:
                    draw.text(
                        (x_cursor + 4, y + 2),
                        _safe(f"{emotion[:4]} {frac:.0%}"),
                        fill=C_WHITE,
                        font=f_xs,
                    )
                x_cursor += seg_w
            y += bar_h + 10

        # ── PROPAGATION PATH ───────────────────────────────────────────────
        draw.text((PAD, y), "PROPAGATION  PATH", fill=C_BLUE_LIGHT, font=f_lg)
        y += 42

        first_post_id = topic_posts[0].id if topic_posts else None
        first_post_time = (
            topic_posts[0].posted_at if topic_posts and topic_posts[0].posted_at else None
        )
        for i, post in enumerate(topic_posts[:6]):
            # Determine action type
            if post.id in introduced_by:
                action     = "INTRODUCED" if post.id == first_post_id else "EXTENDED"
                act_color  = C_ACTION_INTRODUCED if post.id == first_post_id else C_ACTION_EXTENDED
            else:
                action    = "AMPLIFIED"
                act_color = C_ACTION_AMPLIFIED

            # Relative time offset from first post in the path
            rel_time_str = ""
            if i > 0 and first_post_time and post.posted_at:
                delta_sec = (post.posted_at - first_post_time).total_seconds()
                if delta_sec >= 3600:
                    rel_time_str = f"+{int(delta_sec // 3600)}h"
                elif delta_sec >= 60:
                    rel_time_str = f"+{int(delta_sec // 60)}m"
                elif delta_sec >= 0:
                    rel_time_str = "+<1m"

            # Post box
            BOX_H = 78
            self._draw_post_box(
                draw, img, PAD, y, CARD_W - PAD, BOX_H,
                post=post,
                action=action,
                act_color=act_color,
                seq_num=i + 1,
                rel_time_str=rel_time_str,
                f_sm=f_sm,
                f_xs=f_xs,
            )
            y += BOX_H + 4

            # Arrow between posts
            if i < min(len(topic_posts), 6) - 1:
                ax = CARD_W // 2
                draw.line([(ax, y + 1), (ax, y + 16)], fill=C_GREY_MED, width=2)
                draw.polygon(
                    [(ax - 7, y + 12), (ax + 7, y + 12), (ax, y + 22)],
                    fill=C_GREY_MED,
                )
                y += 26

        if not topic_posts:
            draw.text((PAD, y), "No post data available", fill=C_GREY_MED, font=f_md)
            y += 36

        y += 14

        # ── OFFICIAL SOURCES ───────────────────────────────────────────────
        draw.line([(PAD, y), (CARD_W - PAD, y)], fill=C_SEP, width=2)
        y += 14
        draw.text((PAD, y), "OFFICIAL  SOURCES  (RAG)", fill=C_BLUE_LIGHT, font=f_lg)
        y += 42

        if evidence_sources:
            for src in evidence_sources[:6]:
                sname = _safe(src["source_name"] or "Source", 22)
                raw_title = _safe(src["title"] or "")
                stance = src.get("stance", "neutral")
                stance_dot = {
                    "supports": (50, 200, 80),
                    "contradicts": (220, 70, 70),
                    "neutral": (150, 150, 150),
                }.get(stance, C_GREY_MED)

                # Source badge
                bw = max(len(sname) * 11 + 20, 80)
                draw.rectangle(
                    [PAD, y + 2, PAD + bw, y + 28],
                    fill=C_SOURCE_BADGE,
                )
                draw.text((PAD + 10, y + 5), sname, fill=(130, 190, 255), font=f_sm)

                # Stance dot
                draw.ellipse(
                    [PAD + bw + 10, y + 10, PAD + bw + 22, y + 22],
                    fill=stance_dot,
                )

                # Article title — truncate to fit remaining width
                # f_sm ≈ 10px/char at size 18; available width after badge+dot
                title_x = PAD + bw + 30
                avail_title_chars = (CARD_W - title_x - PAD) // 10
                title = raw_title[:max(avail_title_chars, 20)]
                if len(raw_title) > avail_title_chars:
                    title = title.rstrip() + "..."
                draw.text(
                    (title_x, y + 5),
                    title,
                    fill=C_WHITE,
                    font=f_sm,
                )
                y += 36
        else:
            draw.text(
                (PAD, y),
                "No authoritative sources retrieved for this topic.",
                fill=C_GREY_MED, font=f_md,
            )
            y += 36

        # ── VERDICT ────────────────────────────────────────────────────────
        draw.line([(PAD, y), (CARD_W - PAD, y)], fill=C_SEP, width=2)
        y += 14
        verdict_text = (
            f"VERDICT:  {'LIKELY MISINFORMATION' if ts.is_likely_misinfo else 'UNVERIFIED / MONITOR'}"
            f"   |   Risk score: {ts.misinfo_risk:.2f}"
        )
        verdict_color = C_BADGE_MISINFO if ts.is_likely_misinfo else C_GREY_LIGHT
        draw.text((PAD, y), _safe(verdict_text), fill=verdict_color, font=f_md)
        y += 38

        for rc in ts.representative_claims[:2]:
            draw.text(
                (PAD, y),
                _safe(f"\u2022  {rc}", 90),
                fill=C_GREY_LIGHT,
                font=f_sm,
            )
            y += 30

        # ── NARRATIVE MUTATION TIMELINE (Phase 0, Task 1.3) ───────────────
        if NUM_MUTATION >= 2:
            draw.line([(PAD, y), (CARD_W - PAD, y)], fill=C_SEP, width=2)
            y += 14
            draw.text(
                (PAD, y), "NARRATIVE  EVOLUTION  TIMELINE",
                fill=C_BLUE_LIGHT, font=f_md
            )
            y += 32
            prev_x = PAD
            entry_w = (CARD_W - 2 * PAD) // max(NUM_MUTATION, 1)
            for idx, entry in enumerate(mutation_chain[:NUM_MUTATION]):
                ex = PAD + idx * entry_w
                # Connector arrow
                if idx > 0:
                    ax = ex - entry_w // 2
                    draw.line(
                        [(ax - 6, y + 9), (ax + 6, y + 9)],
                        fill=C_GREY_MED, width=2
                    )
                    draw.polygon(
                        [(ax + 4, y + 5), (ax + 4, y + 13), (ax + 10, y + 9)],
                        fill=C_GREY_MED,
                    )
                # Claim snippet
                prop = entry.get("propagation_count") or 1
                snippet = _safe(entry.get("text", "")[:40])
                color = C_GOLD if idx == 0 else C_WHITE
                draw.text(
                    (ex, y),
                    f"#{idx + 1} (×{prop})",
                    fill=C_GREY_LIGHT, font=f_xs
                )
                draw.text((ex, y + 14), snippet, fill=color, font=f_xs)
            y += 26 * NUM_MUTATION + 10

        # ── FOOTER ─────────────────────────────────────────────────────────
        y = CARD_H - 40
        draw.line([(PAD, y), (CARD_W - PAD, y)], fill=C_SEP, width=1)
        draw.text(
            (PAD, y + 8),
            "Society Analysis System  |  Automated Misinformation Detection  |"
            "  Verify before sharing",
            fill=C_GREY_MED,
            font=f_xs,
        )

        # ── Save ───────────────────────────────────────────────────────────
        slug = _safe(ts.label[:20].replace(" ", "_"))
        ts_str = int(time.time())
        filename = f"topic_{slug}_{ts_str}.png"
        out_path = self._output_dir / filename
        img.save(str(out_path), "PNG")
        return str(out_path)

    # ── Drawing helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _badge(
        draw: ImageDraw.ImageDraw,
        x: int, y: int,
        text: str,
        font,
        color: tuple,
    ) -> None:
        w = max(len(text) * 12 + 20, 80)
        draw.rectangle([x, y, x + w, y + 40], fill=color)
        draw.text((x + 10, y + 9), text, fill=C_WHITE, font=font)

    @staticmethod
    def _draw_post_box(
        draw: ImageDraw.ImageDraw,
        img: Image.Image,
        x1: int, y1: int, x2: int, box_h: int,
        post: Post,
        action: str,
        act_color: tuple,
        seq_num: int = 0,
        rel_time_str: str = "",
        f_sm=None,
        f_xs=None,
    ) -> None:
        """Draw a single post entry in the propagation path."""
        # Background panel
        panel = Image.new("RGBA", img.size, (0, 0, 0, 0))
        p_draw = ImageDraw.Draw(panel)
        p_draw.rectangle(
            [x1, y1, x2, y1 + box_h],
            fill=(30, 38, 55, 190),
        )
        img.paste(
            Image.alpha_composite(img.convert("RGBA"), panel).convert("RGB"),
            (0, 0),
        )
        draw = ImageDraw.Draw(img)

        # Left colour bar (action type indicator)
        draw.rectangle([x1, y1, x1 + 7, y1 + box_h], fill=act_color)

        # Username + seq# + relative time + date
        base_name = _safe(f"@{post.channel_name or post.account_id}"[:24])
        seq_part  = f" #{seq_num}" if seq_num else ""
        rel_part  = f" ({rel_time_str})" if rel_time_str else ""
        date_str  = post.posted_at.strftime("%b %d, %H:%M") if post.posted_at else ""
        header    = _safe(f"{base_name}{seq_part}{rel_part}  |  {date_str}")
        draw.text((x1 + 16, y1 + 7), header, fill=(160, 200, 230), font=f_sm)

        # Action badge (right side)
        bx = x2 - 150
        draw.rectangle([bx, y1 + 6, bx + 130, y1 + 29], fill=act_color)
        draw.text((bx + 8, y1 + 9), action, fill=C_WHITE, font=f_xs)

        # Post snippet — wrap to 2 lines so text doesn't overflow
        raw_snippet = _safe(post.text.replace("\n", " ").strip())
        # Available width in the box (chars); f_xs ≈ 8px/char at size 15
        avail_w = (x2 - x1 - 20) // 8
        snippet_lines = textwrap.wrap(raw_snippet, width=max(avail_w, 60))[:2]
        for j, line in enumerate(snippet_lines):
            prefix = '"' if j == 0 else " "
            suffix = '"' if j == len(snippet_lines) - 1 else ""
            draw.text(
                (x1 + 16, y1 + 34 + j * 20),
                f"{prefix}{line}{suffix}",
                fill=(210, 210, 210),
                font=f_xs,
            )

    @staticmethod
    def _topic_bg_prompt(ts: TopicSummary) -> str:
        """Build an SD prompt for the topic background image."""
        label_lower = ts.label.lower()
        if any(w in label_lower for w in ("israel", "war", "military", "nuclear", "conflict")):
            theme = "dark geopolitical map, news network overlay, tension and conflict"
        elif any(w in label_lower for w in ("health", "food", "wellness", "glyphosate")):
            theme = "scientific laboratory, microscope, food safety testing"
        elif any(w in label_lower for w in ("trump", "politic", "maga", "collapse")):
            theme = "american political landscape, government buildings, tension"
        elif any(w in label_lower for w in ("ritter", "interview", "media")):
            theme = "news broadcast studio, microphone, journalist interview"
        else:
            theme = "social media network, digital information flow, abstract data"
        return (
            f"{theme}, cinematic lighting, dark background, "
            "blue tones, no text, high quality, photorealistic"
        )

    @staticmethod
    def _make_background_prompt(claim: Claim) -> str:
        ev = claim.evidence_summary()
        if ev["contradicting"] > ev["supporting"]:
            theme = "fact-checking, investigative journalism, verifying sources"
        else:
            theme = "news media, information, social media"
        return (
            f"Professional infographic background about {theme}, "
            "clean design, blue and white color scheme, "
            "abstract geometric patterns, no text, high quality"
        )
