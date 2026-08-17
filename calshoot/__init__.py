"""CalShoot: one-way calendar sync from TimeTree to Google Calendar."""

import warnings

# google-api-core warns about this interpreter's support window on every
# import. It is not actionable from inside the tool, so keep it out of the way.
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")

__version__ = "0.1.0"
