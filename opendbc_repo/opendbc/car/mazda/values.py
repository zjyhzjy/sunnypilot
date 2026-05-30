from dataclasses import dataclass, field
from enum import IntFlag

from opendbc.car import Bus, CarSpecs, DbcDict, PlatformConfig, Platforms
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.structs import CarParams
from opendbc.car.docs_definitions import CarHarness, CarDocs, CarParts
from opendbc.car.fw_query_definitions import FwQueryConfig, Request, StdQueries

Ecu = CarParams.Ecu


# Steer torque limits

class CarControllerParams:
  # Parameters shared across all Mazda platforms
  STEER_DRIVER_ALLOWANCE = 15     # allowed driver torque before start limiting
  STEER_DRIVER_MULTIPLIER = 1     # weight driver torque
  STEER_DRIVER_FACTOR = 1         # from dbc
  STEER_STEP = 1  # 100 Hz

  def __init__(self, CP):
    # Per-model STEER_MAX and rate limits. Instance-based so each model can be
    # tuned independently. carcontroller.py reads self.params.STEER_MAX (instance)
    # instead of CarControllerParams.STEER_MAX (class) to pick these up.
    #
    # CX-5 2022 EPS (also valid for 2018 CX-5 body + 2022 EPS swap):
    #   STEER_MAX kept at 800 to match pre-compiled panda firmware binary
    #   (panda_h7/main.bin) shipped with sunnypilot staging. Panda safety
    #   limit in mazda.h must be recompiled before raising above 800 —
    #   otherwise panda rejects the command and latches a safety fault
    #   requiring engine restart to clear.
    #   Target once firmware recompiled: STEER_MAX=1400, DELTA_UP=15,
    #   DELTA_DOWN=38 — validated by zephleggett on CX-5 2022 (commit 741f049).
    if CP.carFingerprint == CAR.MAZDA_CX5_2022:
      self.STEER_MAX = 800         # TODO: raise to 1400 after mazda.h recompile
      self.STEER_DELTA_UP = 10     # TODO: raise to 15 after mazda.h recompile
      self.STEER_DELTA_DOWN = 25   # TODO: raise to 38 after mazda.h recompile
    else:
      # All other Mazda platforms — unchanged from official defaults
      self.STEER_MAX = 800         # theoretical max_steer 2047
      self.STEER_DELTA_UP = 10     # torque increase per refresh
      self.STEER_DELTA_DOWN = 25   # torque decrease per refresh


@dataclass
class MazdaCarDocs(CarDocs):
  package: str = "All"
  car_parts: CarParts = field(default_factory=CarParts.common([CarHarness.mazda]))


@dataclass(frozen=True, kw_only=True)
class MazdaCarSpecs(CarSpecs):
  tireStiffnessFactor: float = 0.7  # not optimized yet


class MazdaFlags(IntFlag):
  # Static flags
  # Gen 1 hardware: same CAN messages and same camera
  GEN1 = 1


@dataclass
class MazdaPlatformConfig(PlatformConfig):
  dbc_dict: DbcDict = field(default_factory=lambda: {Bus.pt: 'mazda_2017'})
  flags: int = MazdaFlags.GEN1


class CAR(Platforms):
  MAZDA_CX5 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda CX-5 2017-21")],
    MazdaCarSpecs(mass=3655 * CV.LB_TO_KG, wheelbase=2.7, steerRatio=15.5)
  )
  MAZDA_CX9 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda CX-9 2016-20")],
    MazdaCarSpecs(mass=4217 * CV.LB_TO_KG, wheelbase=3.1, steerRatio=17.6)
  )
  MAZDA_3 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda 3 2017-18")],
    MazdaCarSpecs(mass=2875 * CV.LB_TO_KG, wheelbase=2.7, steerRatio=14.0)
  )
  MAZDA_6 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda 6 2017-20")],
    MazdaCarSpecs(mass=3443 * CV.LB_TO_KG, wheelbase=2.83, steerRatio=15.5)
  )
  MAZDA_CX9_2021 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda CX-9 2021-23", video="https://youtu.be/dA3duO4a0O4")],
    MAZDA_CX9.specs
  )
  MAZDA_CX5_2022 = MazdaPlatformConfig(
    [MazdaCarDocs("Mazda CX-5 2022-25")],
    MAZDA_CX5.specs,
  )


class LKAS_LIMITS:
  STEER_THRESHOLD = 15
  DISABLE_SPEED = 45    # kph
  ENABLE_SPEED = 52     # kph


class Buttons:
  NONE = 0
  SET_PLUS = 1
  SET_MINUS = 2
  RESUME = 3
  CANCEL = 4


FW_QUERY_CONFIG = FwQueryConfig(
  requests=[
    # TODO: check data to ensure ABS does not skip ISO-TP frames on bus 0
    Request(
      [StdQueries.MANUFACTURER_SOFTWARE_VERSION_REQUEST],
      [StdQueries.MANUFACTURER_SOFTWARE_VERSION_RESPONSE],
      bus=0,
    ),
  ],
)

DBC = CAR.create_dbc_map()
