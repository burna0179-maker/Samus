"""Template-recovery workcell — scaffold-fallback orchestration.

When an LLM-driven step fails, retrying the same failing prompt only burns
tokens. This workcell instead returns a pre-validated deterministic template
scaffold so the workflow can continue:

    LLM failure -> template_recovery -> validated scaffold -> continue workflow

Recovery is deterministic, local, cached and constant-time. It consumes
ZERO LLM calls — that is the entire point of the workcell.
"""
