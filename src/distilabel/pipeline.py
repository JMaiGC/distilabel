# Copyright 2023-present, Argilla, Inc.
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

import os
import warnings
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Type,
    TypeVar,
    Union,
)

from datasets import Dataset

from distilabel.dataset import CustomDataset
from distilabel.progress_bar import get_progress_bars_for_pipeline
from distilabel.utils import combine_dicts

if TYPE_CHECKING:
    from distilabel.llm.base import LLM


T = TypeVar("T", bound=Dataset)


class Pipeline(Generic[T]):
    dataset_cls: Type[T] = CustomDataset

    def __init__(
        self,
        generator: Union["LLM", None] = None,
        labeller: Union["LLM", None] = None,
    ) -> None:
        self.generator = generator
        self.labeller = labeller

        if self.generator is None and self.labeller is None:
            raise ValueError("At least one LLM has to be provided to the pipeline")

    def _remap_dataset(self, dataset: Dataset) -> T:
        # Dynamically remaps the `datasets.Dataset` to be an instance of `dataset_cls`
        dataset.__class__ = self.dataset_cls
        return dataset  # type: ignore

    def _validate_dataset(self, dataset: Dataset) -> None:
        # Generation LLM has not been provided, so the columns needed by the Labelling
        # LLM must be in the provided dataset
        if self.labeller is not None:
            if self.generator is None:
                try:
                    self.labeller.task.validate_dataset(dataset.column_names)
                except KeyError as err:
                    raise KeyError(
                        f"Labelling LLM expects a dataset with the following columns: {self.labeller.task.input_args_names}"
                    ) from err
            else:
                expected_columns = (
                    dataset.column_names + self.generator.task.output_args_names
                )
                try:
                    self.labeller.task.validate_dataset(expected_columns)
                except KeyError as err:
                    raise KeyError(
                        f"Labelling LLM expects to receive the following columns after the generation process: {expected_columns}"
                    ) from err

        if self.generator is not None:
            try:
                self.generator.task.validate_dataset(dataset.column_names)
            except KeyError as err:
                raise KeyError(
                    f"Generation LLM expects a dataset with the following columns: {self.generator.task.input_args_names}"
                ) from err

    def _add_columns_to_dataset(
        self,
        dataset: Dataset,
        generations: List[Dict[str, Any]],
        labels: List[Dict[str, Any]],
    ) -> Dataset:
        if self.generator is not None:
            for output_name in self.generator.task.output_args_names:
                dataset = dataset.add_column(
                    output_name, [row.get(output_name, None) for row in generations]
                )

            dataset = dataset.add_column(
                "raw_generation_response",
                [row.get("raw_generation_response", None) for row in generations],
            )

        if self.labeller is not None:
            for output_name in self.labeller.task.output_args_names:
                dataset = dataset.add_column(
                    output_name, [row.get(output_name, None) for row in labels]
                )

            dataset = dataset.add_column(
                "raw_labelling_response",
                [row.get("raw_labelling_response", None) for row in labels],
            )

        return dataset

    def _get_batch_generations(
        self,
        inputs: List[Dict[str, Any]],
        num_generations: int,
        progress_callback_func: Union[Callable, None] = None,
    ) -> List[Dict[str, Any]]:
        batch_generations = self.generator.generate(
            inputs=inputs,
            num_generations=num_generations,
            progress_callback_func=progress_callback_func,
        )

        if self.generator.return_futures:
            batch_generations = [future.result() for future in batch_generations]

        # TODO(alvarobartt): implement fallback mechanism if `combine_dicts` fails
        return [
            {
                "raw_generation_response": raw_responses,
                **combine_dicts(*parsed_responses),
            }
            for raw_responses, parsed_responses in batch_generations
        ]

    def _transform_dataset_to_expected_format(
        self, rows: Dict[str, List[Any]]
    ) -> List[Dict[str, Any]]:
        length = len(next(iter(rows.values())))

        inputs = []
        for i in range(length):
            input = {col: values[i] for col, values in rows.items()}
            inputs.append(input)

        return inputs

    def generate(  # noqa: C901
        self,
        dataset: Dataset,
        num_generations: int = 1,
        batch_size: int = 1,
        display_progress_bar: bool = False,
    ) -> T:
        # We need to convert the dataset to a dict and then back to a dataset
        # as we just want to keep the dataset content, not its metadata
        dataset = Dataset.from_dict(dataset.to_dict())
        self._validate_dataset(dataset)

        generations: List[Dict[str, Any]] = []
        labels: List[Dict[str, Any]] = []

        (
            generation_progress_func,
            labelling_progress_bar,
        ) = get_progress_bars_for_pipeline(
            num_rows=len(dataset),
            num_generations=num_generations,
            display_progress_bar=display_progress_bar,
        )

        for rows in dataset.iter(batch_size=batch_size):
            inputs = self._transform_dataset_to_expected_format(rows)

            if self.generator is not None:
                batch_generations = self._get_batch_generations(
                    inputs, num_generations, generation_progress_func
                )
                generations.extend(batch_generations)

                for input, generations_ in zip(inputs, batch_generations):
                    # Skip the `raw_generation_response` key as not used by the labelling LLM
                    input.update(
                        {
                            k: v
                            for k, v in generations_.items()
                            if k != "raw_generation_response"
                        }
                    )

            if self.labeller is not None:
                # `num_generations` is always 1 because labelling the same input multiple times
                # using the same LLM may not make sense
                batch_labels = self.labeller.generate(
                    inputs=inputs,
                    num_generations=1,
                    progress_callback_func=labelling_progress_bar,
                )
                labels.extend(batch_labels)

        if self.labeller is not None:
            # If the LLM returns futures, we need to wait for them to finish
            if self.labeller.return_futures:
                labels = [future.result() for future in labels]
            # TODO: implement fallback mechanism if `combine_dicts` fails
            formatted_labels = []
            for raw_response, parsed_responses in labels:
                # It is always going to be a list of dicts or lists of dicts
                # TODO: we need to add that to the base definition)
                for parsed_response in parsed_responses:
                    if isinstance(parsed_response, list):
                        formatted_labels.append(
                            {
                                "raw_labelling_response": raw_response,
                                **combine_dicts(*parsed_response),
                            }
                        )
                    elif isinstance(parsed_response, dict):
                        formatted_labels.append(
                            {"raw_labelling_response": raw_response, **parsed_response}
                        )
                    else:
                        raise ValueError(f"Unsupported type: {type(parsed_response)}")

        dataset = self._add_columns_to_dataset(dataset, generations, formatted_labels)
        dataset = self._remap_dataset(dataset)
        # TODO: before releasing check whether we should move the `argilla` export to dataset level e.g. `PreferenceDataset`
        #   that would imply not passing the `task` but just returning the remapped dataset
        if self.labeller is not None:
            dataset.task = self.labeller.task
        return dataset


# TODO: add support for any defined task e.g. pipeline("preference", "ultrafeedback/helpfulness", ...)
def pipeline(
    task: Literal["preference", "critique"],
    subtask: Optional[str] = None,
    *,
    generator: Optional["LLM"] = None,
    labeller: Optional["LLM"] = None,
    **kwargs,
) -> "Pipeline":
    if task == "preference":
        if labeller is None:
            from distilabel.llm.openai_ import OpenAILLM
            from distilabel.tasks.preference.ultrafeedback import UltraFeedbackTask

            task_kwargs = {
                key: kwargs.get(key)
                for key in UltraFeedbackTask.__fields__.keys()  # TODO: update when `pydantic` dependency is removed
                if key in kwargs and not key.startswith("__")
            }

            # Dynamically call the appropriate classmethod using getattr
            if subtask is not None:
                if subtask not in UltraFeedbackTask.__subtasks__:
                    raise ValueError(
                        f"Invalid subtask: {subtask}, available subtasks are {UltraFeedbackTask.__subtasks__}"
                    )
                classmethod_name = f"for_{subtask.lower().replace('-', '_')}"
                if hasattr(UltraFeedbackTask, classmethod_name):
                    classmethod = getattr(UltraFeedbackTask, classmethod_name)

            # TODO: add a logging.info message to inform the user that `OpenAILLM` is being used by default?
            labeller = OpenAILLM(
                model=kwargs.get("openai_model") or "gpt-3.5-turbo",
                task=UltraFeedbackTask(**task_kwargs)
                if subtask is None
                else classmethod(**task_kwargs),
                max_new_tokens=kwargs.get("max_new_tokens") or 256,
                num_threads=kwargs.get("num_threads") or 4,
                openai_api_key=kwargs.get("openai_api_key")
                or os.getenv("OPENAI_API_KEY"),
                temperature=kwargs.get("temperature") or 0.0,
            )
        else:
            from distilabel.tasks.preference.judgelm import JudgeLMTask
            from distilabel.tasks.preference.ultrafeedback import UltraFeedbackTask

            if not isinstance(labeller.task, (UltraFeedbackTask, JudgeLMTask)):
                warnings.warn(
                    f"The `labeller` task for `preference` must be an instance of `UltraFeedbackTask`, got {labeller.task.__class__.__name__}."
                    " If you are planning to use a custom `labeller` for a `preference` task, use it at your own risk, since only `UltraFeedbackTask` is supported at the moment.",
                    UserWarning,
                    stacklevel=2,
                )

        if generator is not None:
            assert (
                generator.task.input_args_names + generator.task.output_args_names
                == labeller.task.input_args_names
            ), (
                f"`generator` outputs do not match `labeller` inputs: "
                f"{generator.task.input_args_names + generator.task.output_args_names} != {labeller.task.input_args_names}"
            )
        from distilabel.dataset import PreferenceDataset

        dataset_cls = PreferenceDataset
    elif task == "critique":
        raise NotImplementedError("Critique task is not implemented yet")
    else:
        raise ValueError(f"Invalid task: {task}")

    class CustomPipeline(Pipeline[dataset_cls]):
        pass

    CustomPipeline.dataset_cls = dataset_cls

    return CustomPipeline(generator=generator, labeller=labeller)