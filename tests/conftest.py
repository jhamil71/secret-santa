import os

# Tests never call outside link-preview services (individual tests turn the fallbacks on against a fake one).
os.environ["LINK_PREVIEW_FALLBACKS"] = "0"
