# Copyright 2022 The T5X Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Train state for passing around objects during training."""

from typing import Any, Mapping, Optional

from flax import optim
import flax.core
from flax.core import scope as flax_scope
from flax.linen import partitioning as flax_partitioning
import flax.serialization
import flax.struct
import jax.numpy as jnp

import typing_extensions

EMPTY_DICT = flax.core.freeze({})
FrozenDict = flax_scope.FrozenDict
FrozenVariableDict = flax_scope.FrozenVariableDict
MutableVariableDict = flax_scope.MutableVariableDict
VariableDict = flax_scope.VariableDict


class TrainState(typing_extensions.Protocol):
  """TrainState interface."""

  @property
  def step(self) -> jnp.ndarray:
    """The current training step as an integer scalar."""
    ...

  @property
  def params(self) -> FrozenVariableDict:
    """The parameters of the model as a PyTree matching the Flax module."""
    ...

  @property
  def param_states(self) -> FrozenVariableDict:
    """The optimizer states of the parameters as a PyTree."""
    ...

  @property
  def flax_mutables(self) -> FrozenVariableDict:
    """Flax mutable collection."""
    ...

  def state_dict(self) -> MutableVariableDict:
    """Returns a mutable representation of the state."""
    ...

  def restore_state(self, state_dict: Mapping[str, Any]) -> 'TrainState':
    """Restores the object state from a state dict."""
    ...

  def replace_params(self, params: VariableDict) -> 'TrainState':
    ...

  def replace_step(self, step: jnp.ndarray) -> 'TrainState':
    ...

  def apply_gradient(self,
                     grads,
                     learning_rate,
                     flax_mutables=EMPTY_DICT) -> 'TrainState':
    """Applies gradient, increments step, and returns an updated TrainState."""
    ...

  def to_logical_axes(self) -> 'TrainState':
    """Replaces `param` and `param-states` with their logical axis names."""
    ...


class FlaxOptimTrainState(flax.struct.PyTreeNode, TrainState):
  """Simple train state for holding parameters, step, optimizer state."""
  _optimizer: optim.Optimizer
  # Contains axis metadata (e.g., names) matching parameter tree.
  params_axes: Optional[FrozenVariableDict] = None
  # Flax mutable fields.
  flax_mutables: FrozenDict = EMPTY_DICT

  @classmethod
  def create(cls, optimizer_def: optim.OptimizerDef,
             model_variables: FrozenVariableDict) -> 'FlaxOptimTrainState':
    other_variables, params = model_variables.pop('params')
    if 'params_axes' in other_variables:
      flax_mutables, params_axes = other_variables.pop('params_axes')
    else:
      params_axes = None
      flax_mutables = other_variables

    # If the optimizer supports `set_param_axes`, then assume that the model
    # code is emitting these axes and use it.
    if hasattr(optimizer_def, 'set_param_axes'):
      if params_axes is None:
        raise ValueError('The optimizer supports params_axes for model-based '
                         'partitioning, but the model is not emitting them.')
      axis_names = flax_partitioning.get_axis_names(params_axes)
      optimizer_def.set_param_axes(axis_names)

    optimizer = optimizer_def.create(params)
    return FlaxOptimTrainState(
        optimizer, params_axes=params_axes, flax_mutables=flax_mutables)

  @property
  def step(self) -> jnp.ndarray:
    return self._optimizer.state.step

  @property
  def params(self) -> FrozenVariableDict:
    return self._optimizer.target

  @property
  def param_states(self) -> FrozenVariableDict:
    return self._optimizer.state.param_states

  def state_dict(self) -> MutableVariableDict:
    state_dict = self._optimizer.state_dict()
    if self.flax_mutables:
      state_dict['flax_mutables'] = flax.core.unfreeze(self.flax_mutables)
    return state_dict

  def apply_gradient(self,
                     grads,
                     learning_rate,
                     flax_mutables=EMPTY_DICT) -> 'FlaxOptimTrainState':
    new_optimizer = self._optimizer.apply_gradient(
        grads, learning_rate=learning_rate)
    return self.replace(_optimizer=new_optimizer, flax_mutables=flax_mutables)

  def replace_params(self, params: VariableDict) -> 'FlaxOptimTrainState':
    return self.replace(_optimizer=self._optimizer.replace(target=params))

  def replace_step(self, step: jnp.ndarray) -> 'FlaxOptimTrainState':
    state_dict = self.state_dict()
    state_dict['state']['step'] = step
    return self.restore_state(state_dict)

  def restore_state(self, state_dict: VariableDict) -> 'FlaxOptimTrainState':
    new_optimizer = self._optimizer.restore_state(state_dict)
    return self.replace(
        _optimizer=new_optimizer,
        flax_mutables=flax.core.freeze(state_dict['flax_mutables'])
        if 'flax_mutables' in state_dict else EMPTY_DICT)

  def as_logical_axes(self) -> 'FlaxOptimTrainState':
    if not hasattr(self._optimizer.optimizer_def, 'derive_logical_axes'):
      raise ValueError(
          f"Optimizer '{self._optimizer.optimizer_def.__class__.__name__}' "
          'requires a `derive_logical_axes` method to be used with named axis '
          'partitioning.')
    return FlaxOptimTrainState(
        _optimizer=self._optimizer.optimizer_def.derive_logical_axes(
            self._optimizer, flax_partitioning.get_axis_names(
                self.params_axes)))
