from .base import RuleSet, State
from .gamedata import GamedataRules
from .translations import PhrasesRules, RadioPanelRules

GENERIC = RuleSet()
RULE_SETS = {
    rule_set.name: rule_set
    for rule_set in (PhrasesRules(), RadioPanelRules(), GamedataRules())
}
