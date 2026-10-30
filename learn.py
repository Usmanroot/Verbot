#!/usr/bin/env python3
"""A tiny, dependency-free logistic-regression learning demo."""

from __future__ import annotations

import math


def sigmoid(value: float) -> float:
    """Numerically stable logistic activation."""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700)))
    exp_value = math.exp(max(value, -700))
    return exp_value / (1.0 + exp_value)


def train_and_gate(epochs: int = 10_000, learning_rate: float = 0.3) -> tuple[list[float], float]:
    """Train a single neuron to learn the linearly separable AND function."""
    if epochs < 1:
        raise ValueError("epochs must be at least 1")
    if learning_rate <= 0:
        raise ValueError("learning_rate must be positive")
    inputs = ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0))
    targets = (0.0, 0.0, 0.0, 1.0)
    weights = [0.0, 0.0]
    bias = 0.0

    for _ in range(epochs):
        for features, target in zip(inputs, targets):
            prediction = sigmoid(sum(x * weight for x, weight in zip(features, weights)) + bias)
            error = prediction - target
            for index, feature in enumerate(features):
                weights[index] -= learning_rate * error * feature
            bias -= learning_rate * error
    return weights, bias


def main() -> int:
    weights, bias = train_and_gate()
    print("AND gate: a one-neuron logistic-regression demo")
    print("This is separate from Verbot's Ollama chat model.\n")
    for features in ((0, 0), (0, 1), (1, 0), (1, 1)):
        probability = sigmoid(sum(x * weight for x, weight in zip(features, weights)) + bias)
        print(f"{features} -> {probability:.3f} (class {int(probability >= 0.5)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
