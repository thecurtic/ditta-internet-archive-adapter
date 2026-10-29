"""Common English words not worth a count request each.

Kept short on purpose: a stopword costs nothing but one polite request, so
this only needs to catch the words nearly every query contains.
"""

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can did do does doing down
    during each few for from further had has have having he her here hers him
    his how i if in into is it its just me more most my no nor not now of off
    on once only or other our ours out over own same she should so some such
    than that the their theirs them then there these they this those through
    to too under until up very was we were what when where which while who
    whom why will with you your yours
    """.split()
)
