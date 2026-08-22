"""Analyzed, but its finding is suppressed via fixtures/.sastrc's
'ignore' criteria (see README's ".sastrc" section). Exists purely to prove
sonar.issue.ignore.multicriteria works end-to-end in this repo's own
ci.yml - do not "fix" the flaw below, and do not move this file without
updating fixtures/.sastrc to match.
"""


def noisy():
    unused = 1
    return 0
