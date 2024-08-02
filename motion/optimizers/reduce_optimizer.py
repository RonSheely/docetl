import json
import random
from typing import Any, Dict, List, Callable, Tuple
from rich.console import Console
from motion.optimizers.utils import LLMClient, extract_jinja_variables
from motion.operations import get_operation
from collections import Counter
from statistics import mean, median
from concurrent.futures import ThreadPoolExecutor, as_completed


class ReduceOptimizer:
    def __init__(
        self,
        config: Dict[str, Any],
        console: Console,
        llm_client: LLMClient,
        max_threads: int,
        run_operation: Callable,
    ):
        self.config = config
        self.console = console
        self.llm_client = llm_client
        self._run_operation = run_operation
        self.max_threads = max_threads

    def optimize(
        self, op_config: Dict[str, Any], input_data: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:

        original_output = self._run_operation(op_config, input_data)

        # Step 1: Synthesize a validator prompt
        validator_prompt = self._generate_validator_prompt(
            op_config, input_data, original_output
        )

        # Step 2: validate the output
        validation_results = self._validate_reduce_output(
            op_config, input_data, original_output, validator_prompt
        )

        # Print the validation results
        self.console.print("[bold]Validation Results:[/bold]")
        if validation_results["needs_improvement"]:
            self.console.print(
                "\n".join(
                    [
                        f"Issues: {result['issues']} Suggestions: {result['suggestions']}"
                        for result in validation_results["validation_results"]
                    ]
                )
            )

            # Step 3: Create and evaluate multiple reduce plans
            reduce_plans = self._create_reduce_plans(op_config, input_data)
            best_plan = self._evaluate_reduce_plans(
                reduce_plans, input_data, validator_prompt
            )

            # Step 4: Run the best reduce plan
            optimized_output = self._run_operation(best_plan, input_data)

            return best_plan, optimized_output
        else:
            self.console.print("No improvements identified.")
            return op_config, original_output

    def _generate_validator_prompt(
        self,
        op_config: Dict[str, Any],
        input_data: List[Dict[str, Any]],
        original_output: List[Dict[str, Any]],
    ) -> str:
        system_prompt = "You are an AI assistant tasked with creating custom validation prompts for reduce operations in data processing pipelines."

        sample_input = random.choice(input_data)
        input_keys = op_config.get("input", {}).get("schema", {})
        if input_keys:
            sample_input = {k: sample_input[k] for k in input_keys}

        reduce_key = op_config.get("reduce_key")
        if reduce_key and original_output:
            key = next(
                (item[reduce_key] for item in original_output if reduce_key in item),
                None,
            )
            sample_output = next(
                (item for item in original_output if item.get(reduce_key) == key), {}
            )
        else:
            sample_output = original_output[0] if original_output else {}

        output_keys = op_config.get("output", {}).get("schema", {})
        sample_output = {k: sample_output[k] for k in output_keys}

        prompt = f"""
        Analyze the following reduce operation and its input/output:

        Reduce Operation Prompt:
        {op_config["prompt"]}

        Sample Input:
        {json.dumps(sample_input, indent=2)}

        Sample Output:
        {json.dumps(sample_output, indent=2)}

        Create a custom validator prompt that will assess how well the reduce operation performed its intended task. The prompt should ask specific questions about the quality and completeness of the output, such as:
        1. Are all input values properly represented in the output?
        2. Is the aggregation performed correctly according to the task requirements?
        3. Is there any loss of important information during the reduction process?
        4. Does the output maintain the required structure and data types?

        Provide your response as a single string containing the custom validator prompt.
        """

        parameters = {
            "type": "object",
            "properties": {"validator_prompt": {"type": "string"}},
            "required": ["validator_prompt"],
        }

        response = self.llm_client.generate(
            [{"role": "user", "content": prompt}],
            system_prompt,
            parameters,
        )
        return json.loads(response.choices[0].message.tool_calls[0].function.arguments)[
            "validator_prompt"
        ]

    def _validate_reduce_output(
        self,
        op_config: Dict[str, Any],
        input_data: List[Dict[str, Any]],
        output_data: List[Dict[str, Any]],
        validator_prompt: str,
        num_samples: int = 5,
    ) -> Dict[str, Any]:
        system_prompt = "You are an AI assistant tasked with validating the output of reduce operations in data processing pipelines."

        # Count occurrences of each key in input_data
        key_counts = {}
        for item in input_data:
            key = item[op_config["reduce_key"]]
            key_counts[key] = key_counts.get(key, 0) + 1

        validation_results = []
        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            futures = []
            for _ in range(num_samples):

                # Select a key weighted by its count
                selected_key = random.choices(
                    list(key_counts.keys()), weights=list(key_counts.values()), k=1
                )[0]

                # Find a sample input with the selected key
                sample_input = next(
                    item
                    for item in input_data
                    if item[op_config["reduce_key"]] == selected_key
                )

                # Find the corresponding output
                sample_output = next(
                    (
                        out
                        for out in output_data
                        if out[op_config["reduce_key"]] == selected_key
                    ),
                    None,
                )

                prompt = f"""
                {validator_prompt}

                Reduce Operation Config:
                {json.dumps(op_config, indent=2)}

                Input Data Sample:
                {json.dumps(sample_input, indent=2)}

                Output Data Sample:
                {json.dumps(sample_output, indent=2)}

                Based on the validator prompt and the input/output samples, assess the quality of the reduce operation output.
                Provide your assessment in the following format:
                """

                parameters = {
                    "type": "object",
                    "properties": {
                        "is_valid": {"type": "boolean"},
                        "issues": {"type": "array", "items": {"type": "string"}},
                        "suggestions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["is_valid", "issues", "suggestions"],
                }

                futures.append(
                    executor.submit(
                        self.llm_client.generate,
                        [{"role": "user", "content": prompt}],
                        system_prompt,
                        parameters,
                    )
                )

            for future in as_completed(futures):
                response = future.result()
                validation_results.append(
                    json.loads(
                        response.choices[0].message.tool_calls[0].function.arguments
                    )
                )

        # Determine if optimization is needed based on validation results
        invalid_count = sum(
            1 for result in validation_results if not result["is_valid"]
        )
        needs_improvement = invalid_count > 1

        return {
            "needs_improvement": needs_improvement,
            "validation_results": validation_results,
        }

    def _create_reduce_plans(
        self, op_config: Dict[str, Any], input_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        reduce_key = op_config["reduce_key"]
        key_counts = Counter(item[reduce_key] for item in input_data)
        values_per_key = list(key_counts.values())

        avg_values = mean(values_per_key)
        median_values = median(values_per_key)
        max_values = max(values_per_key)

        compression_ratio = self._calculate_compression_ratio(op_config, input_data)

        # Print the compression ratio
        self.console.print(
            f"[bold]Estimated Compression Ratio:[/bold] {compression_ratio:.2f}"
        )

        plans = []

        if "fold_prompt" in op_config:
            current_batch_size = op_config.get("fold_batch_size", max_values)
            batch_sizes = [
                max(1, int(current_batch_size * 0.25)),
                max(1, int(current_batch_size * 0.5)),
                max(1, int(current_batch_size * 0.75)),
                current_batch_size,
            ]
            fold_prompt = op_config["fold_prompt"]
        else:
            fold_prompt = self._synthesize_fold_prompt(op_config, input_data)
            batch_sizes = [
                max(1, int(avg_values * 0.5)),
                max(1, int(avg_values)),
                max(1, int(median_values)),
                max(1, int(max_values * 0.5)),
                max_values,
            ]

            # Add compression ratio-based batch size
            # TODO: try batch sizes that are compression_ratio of the p75, p90, p95
            compression_batch_size = max(1, int(compression_ratio * max_values))
            batch_sizes.append(compression_batch_size)

        # Remove duplicates and sort
        batch_sizes = sorted(set(batch_sizes))

        for batch_size in batch_sizes:
            plan = op_config.copy()
            plan["fold_prompt"] = op_config.get("fold_prompt", fold_prompt)
            plan["fold_batch_size"] = batch_size
            plans.append(plan)

        return plans

    def _calculate_compression_ratio(
        self, op_config: Dict[str, Any], input_data: List[Dict[str, Any]]
    ) -> float:
        sample_size = min(100, len(input_data))
        sample_input = random.sample(input_data, sample_size)
        sample_output = self._run_operation(op_config, sample_input)

        reduce_key = op_config["reduce_key"]
        input_schema = op_config.get("input", {}).get("schema", {})
        output_schema = op_config["output"]["schema"]

        compression_ratios = {}
        for key in set(item[reduce_key] for item in sample_input):
            key_input = [item for item in sample_input if item[reduce_key] == key]
            key_output = [item for item in sample_output if item[reduce_key] == key]

            if input_schema:
                key_input_chars = sum(
                    len(json.dumps({k: item[k] for k in input_schema if k in item}))
                    for item in key_input
                )
            else:
                key_input_chars = sum(len(json.dumps(item)) for item in key_input)

            key_output_chars = sum(
                len(json.dumps({k: item[k] for k in output_schema if k in item}))
                for item in key_output
            )

            compression_ratios[key] = (
                key_output_chars / key_input_chars if key_input_chars > 0 else 1
            )

        if not compression_ratios:
            return 1

        # Calculate importance weights based on the number of items for each key
        total_items = sum(
            len([item for item in sample_input if item[reduce_key] == key])
            for key in compression_ratios
        )
        importance_weights = {
            key: len([item for item in sample_input if item[reduce_key] == key])
            / total_items
            for key in compression_ratios
        }

        # Calculate weighted average of compression ratios
        weighted_sum = sum(
            compression_ratios[key] * importance_weights[key]
            for key in compression_ratios
        )
        return weighted_sum

    def _synthesize_fold_prompt(
        self, op_config: Dict[str, Any], input_data: List[Dict[str, Any]]
    ) -> str:
        system_prompt = "You are an AI assistant tasked with creating fold prompts for reduce operations in data processing pipelines."
        original_prompt = op_config["prompt"]

        sample_input = random.sample(input_data, min(5, len(input_data)))
        sample_output = self._run_operation(op_config, sample_input)

        prompt = f"""
        Original Reduce Operation Prompt:
        {original_prompt}

        Sample Input:
        {json.dumps(sample_input, indent=2)}

        Sample Output:
        {json.dumps(sample_output, indent=2)}

        Create a fold prompt for the reduce operation. The fold prompt should:
        1. Minimally modify the original reduce prompt
        2. Describe how to combine the new values with the current reduced value
        3. Be designed to work iteratively, allowing for multiple fold operations. In the first iteration, we will apply the original prompt as is. On subsequent iterations, we will apply the fold prompt to the output of the previous iteration.

        The fold prompt should be a Jinja2 template with the following variables available:
        - {{ output }}: The current reduced value (a dictionary with the current output schema)
        - {{ values }}: A list of new values to be folded in
        - {{ reduce_key }}: The key used for grouping in the reduce operation

        Provide the fold prompt as a single string.
        """

        parameters = {
            "type": "object",
            "properties": {"fold_prompt": {"type": "string"}},
            "required": ["fold_prompt"],
        }

        response = self.llm_client.generate(
            [{"role": "user", "content": prompt}],
            system_prompt,
            parameters,
        )
        return json.loads(response.choices[0].message.tool_calls[0].function.arguments)[
            "fold_prompt"
        ]

    def _evaluate_reduce_plans(
        self,
        plans: List[Dict[str, Any]],
        input_data: List[Dict[str, Any]],
        validator_prompt: str,
    ) -> Dict[str, Any]:
        self.console.print("\n[bold]Evaluating Reduce Plans:[/bold]")
        for i, plan in enumerate(plans):
            self.console.print(f"Plan {i+1} (batch size: {plan['fold_batch_size']})")

        plan_scores = []

        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            futures = [
                executor.submit(
                    self._evaluate_single_plan, plan, input_data, validator_prompt
                )
                for plan in plans
            ]
            for future in as_completed(futures):
                plan, score = future.result()
                plan_scores.append((plan, score))

        # Sort plans by score in descending order, then by fold_batch_size in descending order
        sorted_plans = sorted(
            plan_scores, key=lambda x: (x[1], x[0]["fold_batch_size"]), reverse=True
        )

        self.console.print("\n[bold]Reduce Plan Scores:[/bold]")
        for i, (plan, score) in enumerate(sorted_plans):
            self.console.print(
                f"Plan {i+1} (batch size: {plan['fold_batch_size']}): {score:.2f}"
            )

        best_plan, best_score = sorted_plans[0]
        self.console.print(
            f"\n[green]Selected best plan with score: {best_score:.2f} and batch size: {best_plan['fold_batch_size']}[/green]"
        )

        return best_plan

    def _evaluate_single_plan(
        self,
        plan: Dict[str, Any],
        input_data: List[Dict[str, Any]],
        validator_prompt: str,
    ) -> Tuple[Dict[str, Any], float]:
        output = self._run_operation(plan, input_data)
        validation_result = self._validate_reduce_output(
            plan, input_data, output, validator_prompt, num_samples=5
        )

        # Calculate a score based on validation results
        valid_count = sum(
            1
            for result in validation_result["validation_results"]
            if result["is_valid"]
        )
        score = valid_count / len(validation_result["validation_results"])

        return plan, score