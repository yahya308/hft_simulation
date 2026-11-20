"""Register built-in strategies and explicitly trusted local Python modules."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib import import_module
from math import isfinite
from re import fullmatch
from types import MappingProxyType

from .strategies.bid_only import BidOnly
from .strategies.two_sided import TwoSidedQuote
from .strategy_api import API_VERSION, Strategy


DEFAULT_STRATEGY = "two_sided_quote"
MAX_JSON_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class Parameter:
    type: str
    label: str
    help: str
    default: int | float | bool | str
    minimum: int | float | None = None
    maximum: int | float | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()

    def validate(self, value):
        if self.type == "integer":
            valid = type(value) is int and abs(value) <= MAX_JSON_INTEGER
        elif self.type == "number":
            valid = ((type(value) is int and abs(value) <= MAX_JSON_INTEGER)
                     or (type(value) is float and isfinite(value)))
        elif self.type == "boolean":
            valid = type(value) is bool
        elif self.type == "string":
            valid = type(value) is str
        elif self.type == "enum":
            valid = type(value) is str and value in self.choices
        else:
            valid = False
        if not valid:
            raise ValueError(f"{self.label} için geçerli bir değer gir.")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.label} en az {self.minimum} olmalı.")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.label} en fazla {self.maximum} olmalı.")
        return value

    def validate_schema(self):
        if not all(type(text) is str for text in (self.label, self.help, self.unit)):
            raise ValueError("Parametre adı, açıklaması ve birimi metin olmalı.")
        for bound in (self.minimum, self.maximum):
            if bound is None:
                continue
            if self.type not in ("integer", "number") or type(bound) not in (int, float):
                raise ValueError(f"{self.label} için geçersiz sayısal sınır.")
            if ((type(bound) is float and (not isfinite(bound) or self.type == "integer"))
                    or (type(bound) is int and abs(bound) > MAX_JSON_INTEGER)):
                raise ValueError(f"{self.label} için geçersiz sayısal sınır.")
        if self.type == "enum" and (not isinstance(self.choices, (tuple, list)) or not self.choices
                                    or not all(type(choice) is str for choice in self.choices)
                                    or len(set(self.choices)) != len(self.choices)):
            raise ValueError(f"{self.label} için benzersiz metin seçenekleri tanımla.")
        self.validate(self.default)

    def metadata(self):
        data = {"type": self.type, "label": self.label, "help": self.help,
                "default": self.default, "unit": self.unit}
        if self.minimum is not None:
            data["minimum"] = self.minimum
        if self.maximum is not None:
            data["maximum"] = self.maximum
        if self.type == "enum":
            data["choices"] = list(self.choices)
        return data


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    id: str
    version: str
    api_version: int
    name: str
    description: str
    parameters: Mapping[str, Parameter]
    factory: Callable[..., Strategy]

    def resolve(self, params):
        if not isinstance(params, dict) or params.keys() - self.parameters.keys():
            raise ValueError("Bilinmeyen veya geçersiz strateji parametresi.")
        return {key: spec.validate(params.get(key, spec.default)) for key, spec in self.parameters.items()}

    def create(self, params):
        try:
            instance = self.factory(**params)
            if not callable(getattr(instance, "on_event", None)):
                raise TypeError("on_event işlevi bulunamadı.")
            return instance
        except Exception as error:
            raise ValueError(f"{self.id} stratejisi oluşturulamadı. {error}") from error

    def metadata(self):
        return {"id": self.id, "version": self.version, "api_version": self.api_version,
                "name": self.name, "description": self.description,
                "parameters": {key: spec.metadata() for key, spec in self.parameters.items()}}


class StrategyRegistry:
    def __init__(self, definitions):
        self._definitions = {}
        for definition in definitions:
            if not isinstance(definition, StrategyDefinition):
                raise ValueError("Strateji kaydı bir StrategyDefinition olmalı.")
            if type(definition.id) is not str or not fullmatch(r"[a-z][a-z0-9_-]*", definition.id):
                raise ValueError("Strateji kimliği küçük ASCII harf, rakam, tire veya alt çizgi içermeli.")
            if definition.id in self._definitions:
                raise ValueError(f"Strateji kimliği birden çok kez kayıtlı: {definition.id}")
            if type(definition.api_version) is not int or definition.api_version != API_VERSION:
                raise ValueError(f"{definition.id} uyumsuz strateji API sürümü kullanıyor.")
            if not all(type(value) is str and value.strip()
                       for value in (definition.version, definition.name, definition.description)):
                raise ValueError(f"{definition.id} için sürüm, ad ve açıklama gerekli.")
            if not isinstance(definition.parameters, Mapping) or not callable(definition.factory):
                raise ValueError(f"{definition.id} için parametre şeması ve fabrika gerekli.")
            parameters = {}
            for key, spec in definition.parameters.items():
                if (type(key) is not str or not fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key)
                        or not isinstance(spec, Parameter)):
                    raise ValueError(f"{definition.id} için geçersiz parametre tanımı.")
                spec.validate_schema()
                parameters[key] = replace(spec, choices=tuple(spec.choices)) if spec.type == "enum" else spec
            self._definitions[definition.id] = replace(definition, parameters=MappingProxyType(parameters))

    def get(self, strategy_id):
        if type(strategy_id) is not str or strategy_id not in self._definitions:
            raise ValueError("Kayıtlı bir strateji seç.")
        return self._definitions[strategy_id]

    def catalog(self):
        return {"default_strategy_id": DEFAULT_STRATEGY,
                "strategies": [definition.metadata() for definition in self._definitions.values()]}


def load_registry(module_names=()):
    quantity = Parameter("integer", "Emir miktarı", "Bir pasif emir için hedef pay adedi.",
                         10, 1, 100_000, "adet")
    interval = Parameter("integer", "Yenileme aralığı",
                         "Strateji kararları arasındaki en kısa sanal süre.", 1_000, 1, 3_600_000, "ms")
    spread = Parameter("integer", "Fiyat adımı mesafesi",
                       "Kotasyonun ilgili en iyi kademeden kaç fiyat adımı uzağa konacağı.", 0, 0, 100, "adım")
    definitions = [
        StrategyDefinition(DEFAULT_STRATEGY, "1.0.0", API_VERSION, "Çift taraflı kotasyon",
                           "En iyi alış ve satış kademelerine yakın pasif limit emirleri verir.",
                           {"quantity": quantity, "quote_interval_ms": interval, "spread_ticks": spread},
                           TwoSidedQuote),
        StrategyDefinition("bid_only", "1.0.0", API_VERSION, "Tek taraflı alış",
                           "En iyi alış kademesini izler ve alım koşulu bozulduğunda emrini iptal eder.",
                           {"quantity": quantity, "decision_interval_ms": interval}, BidOnly),
    ]
    for name in module_names:
        try:
            definitions.extend(tuple(import_module(name).get_strategies()))
        except Exception as error:
            raise ValueError(f"{name} strateji modülü yüklenemedi. {error}") from error
    return StrategyRegistry(definitions)
