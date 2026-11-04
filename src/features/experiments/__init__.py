"""Shared machinery for VibeNode's comparison playbooks.

``sessions``   launching VibeNode sessions, following them to their real id,
               reading how they ended, and measuring time, tokens and cost.
               Both playbooks use it.
``compare``    AI Compare: quick side-by-side of AI answers (read-only).
``controlled`` Controlled A/B Test: isolated, repeated, independently verified
               experiments. AI Compare never imports it.
"""
