"""Production organisers; official origin is separate from coverage verification."""
FNL_SOURCE = "fnl_official"
RPL_SOURCE = "rpl_official"
DFL_SOURCE = "bundesliga_official"
PL_SOURCE = "premierleague_official"
EFL_SOURCE = "efl_official"
SA_SOURCE = "seriea_official"
SB_SOURCE = "legab_official"
SOURCE_BY_PREFIX = {"ll:":"laliga_reference", "pl:":PL_SOURCE, "efl:":EFL_SOURCE,
                    "sa:":SA_SOURCE, "sb:":SB_SOURCE, "dfl:":DFL_SOURCE, "fnl:":FNL_SOURCE, "rpl:":RPL_SOURCE}
OFFICIAL_SOURCES = set(SOURCE_BY_PREFIX.values())
OFFICIAL_PREFIXES = tuple(SOURCE_BY_PREFIX)

def source_for_comp(cid):
    return next((source for prefix,source in SOURCE_BY_PREFIX.items() if cid.startswith(prefix)),
                "openligadb" if cid.startswith("ol:") else "skysports_html")
