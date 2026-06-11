import datetime

_PERIODS = [(0,'深夜'),(6,'早上'),(9,'上午'),(12,'中午'),(14,'下午'),(18,'晚上'),(22,'深夜')]

def _period(h):
    p = '深夜'
    for threshold, name in _PERIODS:
        if h >= threshold:
            p = name
    return p

def get_current_time():
    beijing = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    return (f"现在是{beijing.year}年{beijing.month}月{beijing.day}日 "
            f"{_period(beijing.hour)}{beijing.hour}点{beijing.minute:02d}分（北京时间）")

if __name__ == '__main__':
    print(get_current_time())
