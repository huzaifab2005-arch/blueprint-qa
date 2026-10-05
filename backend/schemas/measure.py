import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class ScaleRead(BaseModel):
    index: int
    ratio: float                      # real length / paper length
    text: str                         # 'SCALE: 3/16" = 1'-0"', '1:148 (from 6 dimensions)', 'Calibrated against 12'-7"'
    source: str                       # stated | measured | calibrated
    # verified   stated, and the sheet's dimensions agree
    # measured   no (matching) label, but the dimensions agree on it
    # calibrated set by the user from a known length
    # stated     label only; nothing on the sheet confirms it
    # conflict   stated, but the sheet's dimensions say otherwise
    status: str
    support: int = 0                  # dimensions that agree
    system: str = "imperial"
    calibration_id: uuid.UUID | None = None


class SheetScaleRead(BaseModel):
    page_number: int
    width_pt: float
    height_pt: float
    scales: list[ScaleRead]
    primary: int | None = None
    notes: list[str] = []
    dimension_samples: int = 0


class CalibrationCreate(BaseModel):
    points: list[list[float]] = Field(min_length=2, max_length=2)
    length: str = Field(min_length=1, max_length=64)       # as written: 12'-7", 24", 1200 mm


class MeasurementCreate(BaseModel):
    kind: str = Field(pattern="^(length|polyline|area)$")
    points: list[list[float]] = Field(min_length=2, max_length=500)
    snapped: list[bool] | None = None
    label: str = Field("", max_length=120)
    scale_index: int | None = None


class MeasurementUpdate(BaseModel):
    label: str = Field(max_length=120)


class MeasurementRead(BaseModel):
    id: uuid.UUID
    page_number: int
    kind: str
    label: str
    points: list[list[float]]
    value_in: float | None = None
    value_sqin: float | None = None
    display: str
    display_other: str = ""
    perimeter_display: str | None = None
    uncertainty_in: float = 0.0
    uncertainty_display: str = ""
    scale_ratio: float
    scale_text: str
    scale_status: str
    warnings: list[str] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class SnapRead(BaseModel):
    page_number: int
    count: int
    points: list[list[float]]         # [x, y] as fractions of the displayed page
