"""The pipeline's flows, one module per job. `Pipeline` (pipeline.py) mixes them in, so callers keep
using one object: `pipeline.viral_remix()`, `pipeline.autopilot_day()`, `pipeline.digest()` ...

* trends.py    — trend radar and the content calendar
* viral.py     — viral scout, concept analysis, remixes
* autopilot.py — daily target, spend cap, preview gate, pause
* growth.py    — what our results say (playbook), follow-ups of hits, viewers' requests, title rescue
* community.py — replies to viewers (drafted, approved, sent) and the first comment
* compilation.py — the weekly long-form compilation of the best Shorts (YouTube)
* experiments.py — A/B tests on our own videos: per-video settings, judging, adopting a winner
* sponsors.py — follower counts for the media kit, sponsorship pitches drafted for you
* sound.py     — narration, music, extra-language versions
* reporting.py — post stats, performance feedback, the daily digest
* common.py    — stage constants and shared helpers
"""

from .autopilot import AutopilotFlow
from .common import AUDIO_EXT, BUDGET_HOLDS, ORDER, STOP_STAGES
from .community import CommunityFlow
from .compilation import CompilationFlow
from .experiments import ExperimentFlow
from .growth import GrowthFlow
from .reporting import ReportingFlow
from .sound import SoundFlow
from .sponsors import SponsorFlow
from .trends import TrendFlow
from .viral import ViralFlow

__all__ = ["AUDIO_EXT", "BUDGET_HOLDS", "ORDER", "STOP_STAGES", "AutopilotFlow", "CommunityFlow", "CompilationFlow", "ExperimentFlow", "GrowthFlow", "ReportingFlow", "SoundFlow", "SponsorFlow",
           "TrendFlow", "ViralFlow"]
