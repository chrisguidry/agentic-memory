"""Replaying the record's prompts through the turn path, and the report on it.

A replay reads the prompts a range of the record holds and asks the turn path,
as the code stands, what each one would be handed. It reads the statements
that were live when the prompt was said and writes nothing, so two versions of
the code can be compared on the same prompts, and the pairs they hand out can
be labelled once and reused.
"""
