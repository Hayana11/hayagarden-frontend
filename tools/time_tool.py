import datetime

_DAYS = ['周一','周二','周三','周四','周五','周六','周日']
_PERIODS = [(0,'深夜'),(6,'早上'),(9,'上午'),(12,'中午'),(14,'下午'),(18,'晚上'),(22,'深夜')]

def _period(h):
    p = '深夜'
    for threshold, name in _PERIODS:
        if h >= threshold:
            p = name
    return p

def _fmt(dt):
    return (f"{dt.year}年{dt.month}月{dt.day}日 {_DAYS[dt.weekday()]} "
            f"{_period(dt.hour)}{dt.hour}点{dt.minute:02d}分")

def get_current_time():
    utc = datetime.datetime.utcnow()
    beijing = utc + datetime.timedelta(hours=8)
    osaka   = utc + datetime.timedelta(hours=9)
    return (f"现在北京时间 {_fmt(beijing)}，"
            f"大阪时间 {_fmt(osaka)}（比北京快1小时）")

if __name__ == '__main__':
    print(get_current_time())
