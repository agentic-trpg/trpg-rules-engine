"""Reviewed environmental mechanics; geometry/lifetime use persistent areas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnvironmentalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fog", "magical_darkness", "light"]
    light: Literal["bright", "dim", "dark"] | None = None
    obscurement: Literal["none", "light", "heavy"] = "none"
    sunlight: bool = False
    dim_extension_ft: int = Field(default=0, strict=True, ge=0)
    radius_increase_per_slot_ft: int = Field(default=0, strict=True, ge=0)
    dispels_light_through_level: int | None = Field(default=None, strict=True, ge=0, le=9)
    dispels_darkness_through_level: int | None = Field(default=None, strict=True, ge=0, le=9)
    strong_wind_dispersal: bool = False

    @model_validator(mode="after")
    def coherent_mechanics(self) -> "EnvironmentalSpec":
        if self.kind == "fog":
            valid = (
                self.light is None
                and self.obscurement == "heavy"
                and not self.sunlight
                and not self.dim_extension_ft
                and self.dispels_light_through_level is None
                and self.dispels_darkness_through_level is None
            )
        elif self.kind == "magical_darkness":
            valid = (
                self.light == "dark"
                and self.obscurement == "none"
                and not self.sunlight
                and not self.dim_extension_ft
                and not self.strong_wind_dispersal
                and self.dispels_darkness_through_level is None
            )
        else:
            valid = (
                self.light in ("bright", "dim")
                and self.obscurement == "none"
                and not self.strong_wind_dispersal
                and self.dispels_light_through_level is None
                and (not self.sunlight or self.light == "bright")
                and (not self.dim_extension_ft or self.light == "bright")
            )
        if not valid:
            raise ValueError("inconsistent environmental mechanics")
        return self
