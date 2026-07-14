#!/usr/bin/env python3
# tmp/is_market_open.py
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

def get_good_friday(year):
    # Easter calculation (Computus) - Spencer's algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    # Good Friday is Easter Sunday minus 2 days
    from datetime import date, timedelta
    easter = date(year, month, day)
    return easter - timedelta(days=2)

def is_market_open_today():
    now = datetime.now(CHICAGO)
    today = now.date()
    
    # 1. Weekends
    if today.weekday() >= 5:
        return False
        
    year = today.year
    
    # Define holidays
    holidays = set()
    
    # New Year's Day (Jan 1, observed if weekend)
    ny = datetime(year, 1, 1).date()
    if ny.weekday() == 5: # Sat -> Fri
        holidays.add(datetime(year - 1, 12, 31).date())
    elif ny.weekday() == 6: # Sun -> Mon
        holidays.add(datetime(year, 1, 2).date())
    else:
        holidays.add(ny)
        
    # Martin Luther King Jr. Day (Third Monday in Jan)
    # Washington's Birthday (Third Monday in Feb)
    # Memorial Day (Last Monday in May)
    # Labor Day (First Monday in Sep)
    # Thanksgiving Day (Fourth Thursday in Nov)
    
    # Helper to find Nth weekday of a month
    def get_nth_weekday(year, month, weekday, n):
        # weekday: 0=Mon, 6=Sun
        # n: 1 to 5 (or -1 for last)
        from datetime import date, timedelta
        if n > 0:
            first_day = date(year, month, 1)
            offset = (weekday - first_day.weekday() + 7) % 7
            first_occurrence = first_day + timedelta(days=offset)
            return first_occurrence + timedelta(weeks=n-1)
        else:
            # Last occurrence
            import calendar
            last_day_num = calendar.monthrange(year, month)[1]
            last_day = date(year, month, last_day_num)
            offset = (last_day.weekday() - weekday + 7) % 7
            return last_day - timedelta(days=offset)

    holidays.add(get_nth_weekday(year, 1, 0, 3)) # MLK
    holidays.add(get_nth_weekday(year, 2, 0, 3)) # Presidents' Day
    holidays.add(get_nth_weekday(year, 5, 0, -1)) # Memorial Day
    holidays.add(get_nth_weekday(year, 9, 0, 1)) # Labor Day
    holidays.add(get_nth_weekday(year, 11, 3, 4)) # Thanksgiving
    
    # Juneteenth (June 19, observed if weekend)
    jt = datetime(year, 6, 19).date()
    if jt.weekday() == 5:
        holidays.add(datetime(year, 6, 18).date())
    elif jt.weekday() == 6:
        holidays.add(datetime(year, 6, 20).date())
    else:
        holidays.add(jt)

    # Independence Day (July 4, observed if weekend)
    ind = datetime(year, 7, 4).date()
    if ind.weekday() == 5:
        holidays.add(datetime(year, 7, 3).date())
    elif ind.weekday() == 6:
        holidays.add(datetime(year, 7, 5).date())
    else:
        holidays.add(ind)

    # Christmas Day (Dec 25, observed if weekend)
    xm = datetime(year, 12, 25).date()
    if xm.weekday() == 5:
        holidays.add(datetime(year, 12, 24).date())
    elif xm.weekday() == 6:
        holidays.add(datetime(year, 12, 26).date())
    else:
        holidays.add(xm)

    # Good Friday
    holidays.add(get_good_friday(year))

    if today in holidays:
        return False

    return True

if __name__ == "__main__":
    if is_market_open_today():
        sys.exit(0) # Market is open
    else:
        sys.exit(1) # Market is closed
