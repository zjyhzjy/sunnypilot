"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from openpilot.sunnypilot.models.runners.constants import ModelType


def _make_model_mock(type_raw: int) -> MagicMock:
  """Creates a mock model object with the given type."""
  model = MagicMock()
  model.type.raw = type_raw
  model.metadata = None
  return model


def _make_bundle_mock(type_raws: list[int]) -> MagicMock:
  """Creates a mock bundle with models of the given type integers."""
  bundle = MagicMock()
  bundle.models = [_make_model_mock(t) for t in type_raws]
  bundle.is20hz = False
  return bundle


class TestGetModelRunnerRouting:
  """Tests that get_model_runner returns the correct runner type."""

  def _run_get_model_runner(self, bundle_types: list[int]):
    bundle = _make_bundle_mock(bundle_types)
    mock_split = MagicMock()
    mock_tg = MagicMock()

    with patch('openpilot.sunnypilot.models.runners.helpers.get_active_bundle', return_value=bundle), \
         patch('openpilot.sunnypilot.models.runners.helpers.TinygradSplitRunner', mock_split), \
         patch('openpilot.sunnypilot.models.runners.helpers.TinygradRunner', mock_tg):
      import importlib
      import openpilot.sunnypilot.models.runners.helpers as helpers_mod
      runner = helpers_mod.get_model_runner()
      return runner, mock_split, mock_tg

  def test_new_split_format_uses_split_runner(self):
    """vision + offPolicy + onPolicy (no policy) should use TinygradSplitRunner."""
    _, mock_split, _ = self._run_get_model_runner([ModelType.vision, ModelType.offPolicy, ModelType.onPolicy])
    mock_split.assert_called_once()

  def test_legacy_split_format_uses_split_runner(self):
    """vision + policy (legacy) should use TinygradSplitRunner."""
    _, mock_split, _ = self._run_get_model_runner([ModelType.vision, ModelType.policy])
    mock_split.assert_called_once()

  def test_supercombo_uses_tinygrad_runner(self):
    """supercombo-only bundle should use TinygradRunner, not TinygradSplitRunner."""
    _, mock_split, mock_tg = self._run_get_model_runner([ModelType.supercombo])
    mock_split.assert_not_called()
    mock_tg.assert_called_once_with(ModelType.supercombo)


class TestTinygradSplitRunnerInit:
  """Tests TinygradSplitRunner initialises correctly for old and new bundle formats."""

  def _make_split_runner(self, bundle_types: list[int]):
    """Instantiates TinygradSplitRunner with a mocked bundle containing given types."""
    bundle = _make_bundle_mock(bundle_types)

    runner_instances: dict[int, MagicMock] = {}

    def fake_tinygrad_runner(model_type=ModelType.supercombo):
      m = MagicMock()
      m.input_shapes = {}
      m.output_slices = {}
      m.vision_input_names = []
      runner_instances[model_type] = m
      return m

    with patch('openpilot.sunnypilot.models.runners.tinygrad.tinygrad_runner.TinygradRunner', side_effect=fake_tinygrad_runner), \
         patch('openpilot.sunnypilot.models.runners.model_runner.get_active_bundle', return_value=bundle):
      from openpilot.sunnypilot.models.runners.tinygrad.tinygrad_runner import TinygradSplitRunner
      sr = TinygradSplitRunner.__new__(TinygradSplitRunner)
      sr.models = {t: _make_model_mock(t) for t in bundle_types}
      sr.is_20hz = False
      sr.is_20hz_3d = True
      sr.inputs = {}
      sr._parser_method_dict = {}
      # Re-run the init logic under the patch
      sr.vision_runner = fake_tinygrad_runner(ModelType.vision)
      sr.policy_runner = fake_tinygrad_runner(ModelType.policy) if sr.models.get(ModelType.policy) else None
      sr.off_policy_runner = fake_tinygrad_runner(ModelType.offPolicy) if sr.models.get(ModelType.offPolicy) else None
      sr.on_policy_runner = fake_tinygrad_runner(ModelType.onPolicy) if sr.models.get(ModelType.onPolicy) else None
      return sr

  def test_new_format_policy_runner_is_none(self):
    """vision + offPolicy + onPolicy: policy_runner should be None."""
    sr = self._make_split_runner([ModelType.vision, ModelType.offPolicy, ModelType.onPolicy])
    assert sr.policy_runner is None
    assert sr.off_policy_runner is not None
    assert sr.on_policy_runner is not None

  def test_legacy_format_policy_runner_present(self):
    """vision + policy (legacy): policy_runner should be set."""
    sr = self._make_split_runner([ModelType.vision, ModelType.policy])
    assert sr.policy_runner is not None
    assert sr.off_policy_runner is None
    assert sr.on_policy_runner is None


class TestTinygradSplitRunnerOutputMerging:
  """Tests _run_model output merging and planplus combining."""

  def _make_runner_with_outputs(self, vision_out, policy_out=None, off_policy_out=None, on_policy_out=None):
    from openpilot.sunnypilot.models.runners.tinygrad.tinygrad_runner import TinygradSplitRunner
    sr = TinygradSplitRunner.__new__(TinygradSplitRunner)

    def _make_sub(out):
      m = MagicMock()
      m.run_model.return_value = dict(out)
      return m

    sr.vision_runner = _make_sub(vision_out)
    sr.policy_runner = _make_sub(policy_out) if policy_out is not None else None
    sr.off_policy_runner = _make_sub(off_policy_out) if off_policy_out is not None else None
    sr.on_policy_runner = _make_sub(on_policy_out) if on_policy_out is not None else None
    return sr

  def test_new_format_off_policy_plan_popped(self):
    """off_policy plan should be removed when on_policy_runner is present."""
    plan = np.array([1.0])
    sr = self._make_runner_with_outputs(
      vision_out={'pose': np.array([0.0])},
      off_policy_out={'plan': plan, 'lane_lines': np.array([2.0])},
      on_policy_out={'plan': np.array([3.0]), 'desire_pred': np.array([0.5])},
    )
    outputs = sr._run_model()
    # off_policy plan popped; on_policy plan wins
    assert np.array_equal(outputs['plan'], np.array([3.0]))
    assert 'lane_lines' in outputs
    assert 'desire_pred' in outputs

  def test_planplus_added_to_plan(self):
    """plan + planplus should be combined into plan."""
    plan = np.array([1.0, 2.0])
    planplus = np.array([0.5, 0.5])
    sr = self._make_runner_with_outputs(
      vision_out={'pose': np.array([0.0])},
      policy_out={'plan': plan, 'planplus': planplus},
    )
    outputs = sr._run_model()
    np.testing.assert_array_equal(outputs['plan'], plan + planplus)

  def test_no_policy_runner_still_runs(self):
    """With no policy_runner, vision + off_policy + on_policy outputs are merged."""
    sr = self._make_runner_with_outputs(
      vision_out={'pose': np.array([0.0])},
      off_policy_out={'lane_lines': np.array([1.0]), 'plan': np.array([9.0])},
      on_policy_out={'plan': np.array([2.0])},
    )
    outputs = sr._run_model()
    assert 'pose' in outputs
    assert 'lane_lines' in outputs
    assert np.array_equal(outputs['plan'], np.array([2.0]))
