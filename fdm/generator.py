"""Phase 2: the Pitch Writer drafts a proposal for a shortlisted gig.

Each run saves the proposal as the gig's next version in output/proposals/, beside the older ones
(see fdm.versions).
"""

from __future__ import annotations

import logging

from .agents import run_pitch_writer
from .config import Settings
from .llm import OllamaCloud
from .models import Proposal, ScoredGig
from .store import make_demo_id
from .versions import save_proposal

log = logging.getLogger(__name__)


async def generate_proposal(llm: OllamaCloud, scored: ScoredGig, settings: Settings) -> Proposal:
    """Write a new proposal for the gig, keeping the older ones.

    Raises LLMError if no usable proposal was written; nothing is saved then.
    """
    gig = scored.gig
    demo_id = make_demo_id(gig)
    log.info("[%s] Pitch Writer is drafting the proposal", demo_id)
    pitch, model = await run_pitch_writer(llm, gig, freelancer_name=settings.freelancer_name,
                                          freelancer_profile=settings.freelancer_profile)
    saved = save_proposal(settings.output_dir, demo_id, pitch.message + "\n", {
        "model": model, "bid_usd": pitch.bid_usd, "pay_type": gig.pay_type, "delivery_days": pitch.delivery_days,
    })
    log.info("[%s] Saved %s", demo_id, saved.path)
    return Proposal(demo_id, scored, pitch, model, saved.path, saved.number)
