"""Spending categories (docs/notify.md#分类规则, #ai-分类).

rules.py     keyword rules: your rules.yaml first, then the built-in default_rules.yaml
ai.py        merchants the rules miss, asked once of an AI and the answers stored
provider.py  the interface an AI provider implements, and choosing one (ai.provider)
deepseek.py  the provider for DeepSeek, or any API speaking the Anthropic Messages format
names.py     merchant names without the banks' payment markers
"""
