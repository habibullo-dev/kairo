"""Reviewed, local role-playbook packs for orchestration workers.

Packs are deliberately configuration, not tools or authority.  They are loaded only from a
fixed local directory, hash-pinned by settings, and compile to process guidance for a scoped
member.  Model output, retrieved content, and user messages have no path into this package.
"""

from kira.skills.catalog import (
    CompiledSkills,
    MemberIdentity,
    SkillCatalog,
    SkillPackError,
)

__all__ = ["CompiledSkills", "MemberIdentity", "SkillCatalog", "SkillPackError"]
