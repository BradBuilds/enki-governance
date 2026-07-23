"""Document ingestion + mention extraction subsystem.

Backed by migration 053. Implements the hierarchical-collapse model:
  documents → sections → pages → paragraphs → mentions → statements
            \\
             issues (collapse_candidate, contradiction, ...)

See:
  the internal refdata hierarchical collapse plan
"""
