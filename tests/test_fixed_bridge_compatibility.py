"""An older bridge cannot silently omit the root policy and weaken a repair."""
from __future__ import annotations

from unittest.mock import patch
import pytest

from dwg_semantic.modeling_standard import VERSION
from dwg_semantic.sketchup_live import (SketchUpLiveError, apply_live_repair_plan,
                                        inspect_active_white_model)
from test_fixed_live_standard import add_plan


def health(version='2.0',standard=VERSION):
    return {'protocol':'dwg-live-repair','protocol_version':version,
            'fixed_modeling_standard_version':standard}


@pytest.mark.parametrize('old_health',[health('1.0'),health(standard=None),{}])
def test_unsupported_bridge_is_rejected_before_inspect_or_apply(old_health):
    plan=add_plan()
    with patch('dwg_semantic.sketchup_live._call',return_value=old_health)as remote:
        with pytest.raises(SketchUpLiveError,match='live-install'):
            inspect_active_white_model()
        assert [call.args[0]for call in remote.call_args_list]==['health']
    with patch('dwg_semantic.sketchup_live._call',return_value=old_health)as remote:
        with pytest.raises(SketchUpLiveError,match='live-install'):
            apply_live_repair_plan(plan,confirmation_sha256=plan['plan_sha256'])
        assert [call.args[0]for call in remote.call_args_list]==['health']


def test_current_bridge_performs_capability_handshake_before_each_operation():
    plan=add_plan()
    with patch('dwg_semantic.sketchup_live._call',side_effect=[health(),{'snapshot':True}])as remote:
        assert inspect_active_white_model()=={'snapshot':True}
        assert [call.args[0]for call in remote.call_args_list]==['health','inspect_white_model']
    with patch('dwg_semantic.sketchup_live._call',side_effect=[health(),{'applied':True}])as remote:
        assert apply_live_repair_plan(plan,confirmation_sha256=plan['plan_sha256'])=={'applied':True}
        assert [call.args[0]for call in remote.call_args_list]==['health','apply_live_repair']
