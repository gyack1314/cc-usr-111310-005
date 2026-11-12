"""核对原始配置及数量，不执行仓库变换。"""
from .io import integer,unique
def summarize(v,root):
    unique(v['records'],'record_id');unique(v['requests'],'request_id');unique(v['features'],'name')
    for f in v['features']:
        integer(f['ttl'])
        if f['delete_policy'] not in ('fallback','missing'):raise ValueError('删除策略未知')
    return {'records':len(v['records']),'requests':len(v['requests']),'features':len(v['features'])}
