import hashlib
from sqlalchemy import select
from .models import Grant,GrantBatch,GrantBatchItem,Treatment,Accession,Isolation,Encumbrance,Audit,now
from .catalog_service import reviewed_risk,latest_observation
from .common import get,reject,dump

RISK_ORDER={'high':3,'medium':2,'low':1}

def ineligible_reason(db,plan):
    """返回草案不能入选的原因；空串表示仍具备入选资格。"""
    if plan.status!='draft':
        return f'方案状态已变为{plan.status}，不再是草案'
    folio=db.get(Accession,plan.accession_id)
    if not folio:
        return '卷册记录不存在'
    if not db.scalar(select(Isolation).where(Isolation.accession_id==folio.id,Isolation.active==True)):
        return '卷册已不在隔离柜'
    risk=reviewed_risk(db,folio.id)
    if not risk:
        return '最新病害风险未经主管复核'
    if db.scalar(select(Encumbrance).where(Encumbrance.treatment_id==plan.id,Encumbrance.status=='held')) is not None:
        return '方案已被其他批次或单笔占款占用'
    return ''

def universe_fingerprint(db):
    """覆盖全部草案的资格口径指纹；复测、撤销、占款等任一变化都会改变。"""
    parts=[]
    plans=db.scalars(select(Treatment).where(Treatment.status=='draft').order_by(Treatment.id)).all()
    for plan in plans:
        folio=db.get(Accession,plan.accession_id)
        in_cabinet=bool(folio and db.scalar(select(Isolation).where(Isolation.accession_id==plan.accession_id,Isolation.active==True)))
        risk=reviewed_risk(db,plan.accession_id) if folio else None
        held=db.scalar(select(Encumbrance).where(Encumbrance.treatment_id==plan.id,Encumbrance.status=='held')) is not None
        latest=latest_observation(db,plan.accession_id) if folio else None
        obs_id=latest.id if latest else 0
        parts.append('|'.join(map(str,(plan.id,plan.estimated_cents,in_cabinet,risk.approved if risk else '',obs_id,held))))
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()

def collect(db,grant):
    """编制全部草案：高风险优先，同风险估价低者优先，再按方案号稳定排序。

    资格不符（未复核/已出柜/已占款/非草案）的草案同样落为落选行并保留原因，
    排在具备资格者之后，确保任何草案都不会在名单中无声消失。
    """
    eligible=[];ineligible=[]
    for plan in db.scalars(select(Treatment).order_by(Treatment.id)).all():
        reason=ineligible_reason(db,plan)
        if reason:
            ineligible.append({'plan':plan,'risk':'','estimate':plan.estimated_cents,
                               'decision':'excluded','reason':reason})
            continue
        risk=reviewed_risk(db,plan.accession_id)
        eligible.append({'plan':plan,'risk':risk.approved,'estimate':plan.estimated_cents})
    eligible.sort(key=lambda r:(-RISK_ORDER[r['risk']],r['estimate'],r['plan'].id))
    available=grant.budget_cents-grant.reserved_cents-grant.spent_cents
    remaining=available
    result=[]
    for rank,row in enumerate(eligible,1):
        plan=row['plan']
        if row['estimate']<=remaining:
            result.append({'plan':plan,'rank':rank,'risk':row['risk'],'estimate':row['estimate'],
                           'decision':'candidate','reason':''})
            remaining-=row['estimate']
        else:
            result.append({'plan':plan,'rank':rank,'risk':row['risk'],'estimate':row['estimate'],
                           'decision':'excluded','reason':f'整笔估价 {row["estimate"]} 分超出当时剩余余额 {remaining} 分，不得拆分工序'})
    for offset,row in enumerate(ineligible,1):
        row['rank']=len(eligible)+offset
        result.append(row)
    return result,available

def build_preview(db,data,user):
    grant=get(db,Grant,data.grant_id,True)
    rows,available=collect(db,grant)
    reject(not rows,'没有可编制的修复方案，请先建立方案')
    batch=GrantBatch(grant_id=grant.id,status='preview',fingerprint=universe_fingerprint(db),available_cents=available)
    db.add(batch);db.flush()
    for row in rows:
        db.add(GrantBatchItem(batch_id=batch.id,treatment_id=row['plan'].id,rank_no=row['rank'],
                              risk_level=row['risk'],estimated_cents=row['estimate'],
                              decision=row['decision'],reason=row['reason']))
    db.flush()
    audit_detail={'批次':batch.id,'入选':sum(1 for r in rows if r['decision']=='candidate'),
                  '落选':sum(1 for r in rows if r['decision']=='excluded')}
    db.add(Audit(actor=user.username,action='编制专项资助名单（草案）',detail=str(audit_detail)[:300]))
    db.commit()
    return batch

def serialize_batch(db,batch):
    items=db.scalars(select(GrantBatchItem).where(GrantBatchItem.batch_id==batch.id).order_by(GrantBatchItem.rank_no)).all()
    payload=dump(batch)
    payload['items']=[]
    for item in items:
        plan=db.get(Treatment,item.treatment_id)
        folio=db.get(Accession,plan.accession_id) if plan else None
        payload['items'].append({**dump(item),
                                 'treatment_name':plan.name if plan else '（已删除方案）',
                                 'accession_code':folio.code if folio else '',
                                 'accession_title':folio.title if folio else ''})
    return payload

def confirm_batch(db,batch_id,data,user):
    # 先无锁读取批次；幂等重试直接回传已确认结果，不重复占款。
    existing=db.scalar(select(GrantBatch).where(GrantBatch.id==batch_id))
    reject(existing is None,'资助名单不存在，请重新编制')
    if existing.status=='confirmed':
        return serialize_batch(db,existing),True
    # 先锁批次行，再按固定顺序锁卷册与方案，最后锁专项；与单笔占款同一加锁次序，避免并发死锁。
    batch=get(db,GrantBatch,batch_id,True)
    reject(batch.status!='preview','该名单已处理，请刷新查看结果')
    items=db.scalars(select(GrantBatchItem).where(GrantBatchItem.batch_id==batch.id).order_by(GrantBatchItem.rank_no)).all()
    plan_ids=sorted(i.treatment_id for i in items)
    accession_ids=sorted({db.get(Treatment,pid).accession_id for pid in plan_ids})
    folios={aid:get(db,Accession,aid,True) for aid in accession_ids}
    plans={pid:get(db,Treatment,pid,True) for pid in plan_ids}
    grant=get(db,Grant,batch.grant_id,True)
    # 持锁后复核：名单确认前若发生复测、撤销或先占款，整批拒绝沿用旧名单。
    reject(universe_fingerprint(db)!=batch.fingerprint,'名单依据已变化（复测/撤销/占款等），整批作废，请重新编制名单')
    remaining=grant.budget_cents-grant.reserved_cents-grant.spent_cents
    funded_total=0
    for item in items:
        plan=plans[item.treatment_id]
        if item.decision=='excluded':
            continue
        reason=ineligible_reason(db,plan)
        if reason:
            # 指纹已应拦截；此处为防御性落档，保证落选原因不丢失。
            item.decision='excluded';item.reason=reason
            continue
        if plan.estimated_cents<=remaining:
            grant.reserved_cents+=plan.estimated_cents
            remaining-=plan.estimated_cents;funded_total+=plan.estimated_cents
            db.add(Encumbrance(treatment_id=plan.id,grant_id=grant.id,amount_cents=plan.estimated_cents))
            plan.status='funded'
            folios[plan.accession_id].status='repair'
            item.decision='funded';item.reason=''
        else:
            item.decision='excluded'
            item.reason=f'确认时剩余余额 {remaining} 分不足整笔占用 {plan.estimated_cents} 分，留待下批'
    batch.status='confirmed';batch.funded_cents=funded_total;batch.balance_cents=remaining
    batch.note=(data.note or '')[:200]
    batch.confirmed_at=now()
    db.flush()
    db.add(Audit(actor=user.username,action='确认专项资助名单',detail=f'批次 {batch.id} 占款 {funded_total} 分'))
    db.commit()
    return serialize_batch(db,batch),False
