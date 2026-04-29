"""Automated version of the v3 manual QA playbook.

Scenarios live in :mod:`tests.manual_playbook.scenarios`. Each scenario is
graded by structured assertions (not string matches) so regressions surface
clearly and live-provider runs produce the same kind of verdict the
hand-written QA report used to produce.
"""
