"""The pipeline's flows, one module per job. `Pipeline` (pipeline.py) mixes them in, so callers keep
using one object: `pipeline.viral_remix()`, `pipeline.autopilot_day()`, `pipeline.digest()` ...

* trends.py    — trend radar and the content calendar
* viral.py     — viral scout, concept analysis, remixes
* autopilot.py — daily target, spend cap, preview gate, pause
* growth.py    — what our results say (playbook), follow-ups of hits, viewers' requests, title rescue
* community.py — replies to viewers (drafted, approved, sent) and the first comment
* sound.py     — narration, music, extra-language versions
* reporting.py — post stats, performance feedback, the daily digest
* common.py    — stage constants and shared helpers
"""

from .autopilot import AutopilotFlow
from .common import AUDIO_EXT, BUDGET_HOLDS, ORDER, STOP_STAGES
from .community import CommunityFlow
from .growth import GrowthFlow
from .reporting import ReportingFlow
from .sound import SoundFlow
from .trends import TrendFlow
from .viral import ViralFlow

__all__ = ["AUDIO_EXT", "BUDGET_HOLDS", "ORDER", "STOP_STAGES", "AutopilotFlow", "CommunityFlow", "GrowthFlow", "ReportingFlow", "SoundFlow",
           "TrendFlow", "ViralFlow"]
