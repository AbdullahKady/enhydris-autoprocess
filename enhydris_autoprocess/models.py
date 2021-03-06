import csv
import datetime as dt
import re
from io import StringIO

from django.db import IntegrityError, models, transaction
from django.utils.translation import gettext_lazy as _

import numpy as np
import pandas as pd
from haggregate import aggregate, regularize

from enhydris.models import Timeseries, TimeseriesGroup

from . import tasks


class AutoProcess(models.Model):
    timeseries_group = models.ForeignKey(TimeseriesGroup, on_delete=models.CASCADE)

    class Meta:
        verbose_name_plural = _("Auto processes")

    def execute(self):
        self.htimeseries = self._subclass.source_timeseries.get_data(
            start_date=self._get_start_date()
        )
        result = self._subclass.process_timeseries()
        self._subclass.target_timeseries.append_data(result)

    @property
    def _subclass(self):
        """Return the AutoProcess subclass for this instance.

        AutoProcess is essentially an abstract base class; its instances are always one
        of its subclasses, i.e. Checks, CurveInterpolation, or Aggregation. Sometimes we
        might have an AutoProcess instance without yet knowing what subclass it is; in
        that case, "myinstance._subclass" is the subclass.

        The method works by following the reverse implied one-to-one relationships
        created by Django when using multi-table inheritance. If auto_process is an
        AutoProcess object and there exists a related Checks object, this is accessible
        as auto_process.checks. So by checking whether the auto_process object ("self"
        in this case) has a "checks" (or "curveinterpolation", or "aggregation")
        attribute, we can figure out what the actual subclass is.
        """
        for alternative in ("checks", "curveinterpolation", "aggregation"):
            if hasattr(self, alternative):
                result = getattr(self, alternative)
                if hasattr(self, "htimeseries"):
                    result.htimeseries = self.htimeseries
                return result

    def _get_start_date(self):
        start_date = self._subclass.target_timeseries.end_date
        if start_date:
            start_date += dt.timedelta(minutes=1)
        return start_date

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        self._subclass._check_integrity()
        transaction.on_commit(
            lambda: tasks.execute_auto_process.apply_async(args=[self.id])
        )
        return result

    def _check_integrity(self):
        pass

    @property
    def source_timeseries(self):
        return self._subclass.source_timeseries

    @property
    def target_timeseries(self):
        return self._subclass.target_timeseries


class Checks(AutoProcess):
    def __str__(self):
        return _("Checks for {}").format(str(self.timeseries_group))

    @property
    def source_timeseries(self):
        obj, created = self.timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.RAW
        )
        return obj

    @property
    def target_timeseries(self):
        obj, created = self.timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.CHECKED
        )
        return obj

    def process_timeseries(self):
        for check_type in (RangeCheck,):
            try:
                check = check_type.objects.get(checks=self)
                check.htimeseries = self.htimeseries
                check.check_timeseries()
                self.htimeseries = check.htimeseries
            except check.DoesNotExist:
                pass
        return self.htimeseries.data


class RangeCheck(models.Model):
    checks = models.OneToOneField(Checks, on_delete=models.CASCADE, primary_key=True)
    upper_bound = models.FloatField()
    lower_bound = models.FloatField()
    soft_upper_bound = models.FloatField(blank=True, null=True)
    soft_lower_bound = models.FloatField(blank=True, null=True)

    def __str__(self):
        return _("Range check for {}").format(str(self.checks.timeseries_group))

    def check_timeseries(self):
        self._do_hard_limits()
        self._do_soft_limits()

    def _do_hard_limits(self):
        self._find_out_of_bounds_values(self.lower_bound, self.upper_bound)
        self._replace_out_of_bounds_values_with_nan()
        self._add_flag_to_out_of_bounds_values("RANGE")

    def _do_soft_limits(self):
        self._find_out_of_bounds_values(self.soft_lower_bound, self.soft_upper_bound)
        self._add_flag_to_out_of_bounds_values("SUSPECT")

    def _find_out_of_bounds_values(self, low, high):
        timeseries = self.htimeseries.data
        self.out_of_bounds_mask = ~pd.isnull(timeseries["value"]) & ~timeseries[
            "value"
        ].between(low, high)

    def _replace_out_of_bounds_values_with_nan(self):
        self.htimeseries.data.loc[self.out_of_bounds_mask, "value"] = np.nan

    def _add_flag_to_out_of_bounds_values(self, flag):
        d = self.htimeseries.data
        out_of_bounds_with_flags_mask = self.out_of_bounds_mask & (d["flags"] != "")
        d.loc[out_of_bounds_with_flags_mask, "flags"] += " "
        d.loc[self.out_of_bounds_mask, "flags"] += flag


class CurveInterpolation(AutoProcess):
    target_timeseries_group = models.ForeignKey(
        TimeseriesGroup, on_delete=models.CASCADE
    )

    def __str__(self):
        return f"=> {self.target_timeseries_group}"

    @property
    def source_timeseries(self):
        try:
            return self.timeseries_group.timeseries_set.get(type=Timeseries.CHECKED)
        except Timeseries.DoesNotExist:
            pass
        obj, created = self.timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.RAW
        )
        return obj

    @property
    def target_timeseries(self):
        obj, created = self.target_timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.PROCESSED
        )
        return obj

    def process_timeseries(self):
        timeseries = self.htimeseries.data
        for period in self.curveperiod_set.order_by("start_date"):
            x, y = period._get_curve()
            start, end = period.start_date, period.end_date
            values_array = timeseries.loc[start:end, "value"].values
            new_array = np.interp(values_array, x, y, left=np.nan, right=np.nan)
            timeseries.loc[start:end, "value"] = new_array
            timeseries.loc[start:end, "flags"] = ""
        return timeseries


class CurvePeriod(models.Model):
    curve_interpolation = models.ForeignKey(
        CurveInterpolation, on_delete=models.CASCADE
    )
    start_date = models.DateField()
    end_date = models.DateField()

    def __str__(self):
        return "{}: {} - {}".format(
            str(self.curve_interpolation), self.start_date, self.end_date
        )

    def _get_curve(self):
        x = []
        y = []
        for point in self.curvepoint_set.filter(curve_period=self).order_by("x"):
            x.append(point.x)
            y.append(point.y)
        return x, y

    def set_curve(self, s):
        """Replaces all existing points with ones read from a string.

        The string can be comma-delimited or tab-delimited, or a mix.
        """

        s = s.replace("\t", ",")
        self.curvepoint_set.all().delete()
        for row in csv.reader(StringIO(s)):
            x, y = [float(item) for item in row[:2]]
            CurvePoint.objects.create(curve_period=self, x=x, y=y)


class CurvePoint(models.Model):
    curve_period = models.ForeignKey(CurvePeriod, on_delete=models.CASCADE)
    x = models.FloatField()
    y = models.FloatField()

    def __str__(self):
        return _("{}: Point ({}, {})").format(str(self.curve_period), self.x, self.y)


class Aggregation(AutoProcess):
    METHOD_CHOICES = [
        ("sum", "Sum"),
        ("mean", "Mean"),
        ("max", "Max"),
        ("min", "Min"),
    ]
    target_time_step = models.CharField(
        max_length=7,
        help_text=_(
            'E.g. "10min", "H" (hourly), "D" (daily), "M" (monthly), "Y" (yearly). '
            "More specifically, it's an optional number plus a unit, with no space in "
            "between. The units available are min, H, D, M, Y."
        ),
    )
    method = models.CharField(max_length=4, choices=METHOD_CHOICES)
    max_missing = models.PositiveSmallIntegerField(
        default=0,
        help_text=(
            "Defines what happens if some of the source records corresponding to a "
            "destination record are missing. Suppose you are aggregating ten-minute "
            "to hourly and for 23 January between 12:00 and 13:00 there are only "
            "four nonempty records in the ten-minute time series (instead of the "
            "usual six). If you set this to 1 or lower, the hourly record for 23 "
            "January 13:00 will be empty; if 2 or larger, the hourly value will be "
            "derived from the four values. In the latter case, the MISS flag will "
            "also be set in the resulting record."
        ),
    )
    resulting_timestamp_offset = models.CharField(
        max_length=7,
        blank=True,
        help_text=(
            'If the time step of the target time series is one day ("D") and you set '
            'the resulting timestamp offset to "1min", the resulting time stamps will '
            "be ending in 23:59.  This does not modify the calculations; it only "
            "subtracts the specified offset from the timestamp after the calculations "
            "have finished. Leave empty to leave the timestamps alone."
        ),
    )

    def __str__(self):
        return _("Aggregation for {}").format(str(self.timeseries_group))

    @property
    def source_timeseries(self):
        try:
            return self.timeseries_group.timeseries_set.get(type=Timeseries.CHECKED)
        except Timeseries.DoesNotExist:
            pass
        obj, created = self.timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.RAW
        )
        return obj

    @property
    def target_timeseries(self):
        obj, created = self.timeseries_group.timeseries_set.get_or_create(
            type=Timeseries.AGGREGATED, time_step=self.target_time_step
        )
        return obj

    def save(self, force_insert=False, force_update=False, *args, **kwargs):
        self._check_resulting_timestamp_offset()
        super().save(force_insert, force_update, *args, **kwargs)

    def _check_resulting_timestamp_offset(self):
        if not self.resulting_timestamp_offset:
            return
        else:
            self._check_nonempty_resulting_timestamp_offset()

    def _check_nonempty_resulting_timestamp_offset(self):
        m = re.match(r"(-?)(\d*)(.*)$", self.resulting_timestamp_offset)
        sign, number, unit = m.group(1, 2, 3)
        if unit != "min" or (sign == "-" and number == ""):
            raise IntegrityError(
                '"{}" is not a valid resulting time step offset.'.format(
                    self.resulting_timestamp_offset
                )
            )

    def process_timeseries(self):
        self.source_end_date = self.htimeseries.data.index[-1]
        self._regularize_time_series()
        self._aggregate_time_series()
        self._trim_last_record_if_not_complete()
        return self.htimeseries

    def _regularize_time_series(self):
        self.htimeseries = regularize(self.htimeseries, new_date_flag="DATEINSERT")

    def _aggregate_time_series(self):
        source_step = self._get_source_step()
        target_step = self._get_target_step()
        min_count = (
            self._divide_target_step_by_source_step(source_step, target_step)
            - self.max_missing
        )
        min_count = max(min_count, 1)
        self.htimeseries = aggregate(
            self.htimeseries,
            target_step,
            self.method,
            min_count=min_count,
            target_timestamp_offset=self.resulting_timestamp_offset or None,
        )

    def _get_source_step(self):
        return pd.infer_freq(self.htimeseries.data.index)

    def _get_target_step(self):
        result = self.target_timeseries.time_step
        if not result[0].isdigit():
            result = "1" + result
        return result

    def _divide_target_step_by_source_step(self, source_step, target_step):
        return int(
            pd.Timedelta(target_step) / pd.tseries.frequencies.to_offset(source_step)
        )

    def _trim_last_record_if_not_complete(self):
        # If the very last record of the time series has the "MISS" flag, it means it
        # was derived with one or more missing values in the source.  We don't want to
        # leave such a record at the end of the target time series, or it won't be
        # re-calculated when more data becomes available, because processing begins at
        # the record following the last existing one.
        if self._last_target_record_needs_trimming():
            self.htimeseries.data = self.htimeseries.data[:-1]

    def _last_target_record_needs_trimming(self):
        if len(self.htimeseries.data.index) == 0:
            return False
        last_target_record = self.htimeseries.data.iloc[-1]
        last_target_record_date = last_target_record.name + pd.Timedelta(
            self.resulting_timestamp_offset
        )
        return (
            "MISS" in last_target_record["flags"]
            and self.source_end_date < last_target_record_date
        )
