"""Excluded from analysis entirely via fixtures/.sastignore (see README's
".sastignore" section). Exists purely to prove path exclusion works
end-to-end in this repo's own ci.yml - do not "fix" the flaw below, and do
not move this file without updating fixtures/.sastignore to match.
"""


def noisy():
    unused = 1
    return 0
