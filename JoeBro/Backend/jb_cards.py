"""Compact chat visuals built from successful, structured tool results only."""
import hashlib
import json
import re

KINDS={'calendar','weather','tasks','reminders','shopping','agents','memory','health','research','email','plan','integration','review'}


def _text(value, limit=180):
    return str(value if value is not None else '')[:limit]


def _row(title, detail='', state=''):
    return {'title':_text(title),'detail':_text(detail,240),'state':_text(state,30)}


def _json(output):
    if isinstance(output,(dict,list)): return output
    try:return json.loads(output)
    except (ValueError,TypeError):return None


def tool_visualization(name, args, output, exit_code=0):
    if exit_code != 0:return None
    a=args if isinstance(args,dict) else {}
    d=_json(output)
    if isinstance(d,dict) and (d.get('ok') is False or d.get('error') or d.get('isError')):return None
    data=d if isinstance(d,dict) else {}
    leaf=name.split('__')[-1]
    action=str(a.get('action') or '').lower()
    # Routine observations belong in the tool card, never in a result graphic.
    if leaf in ('list_emails','muse_recall','muse_remember','muse_update','muse_forget',
                'manage_memory','manage_agents','coding_agents','agent_list_sessions','agent_inspect_session'):
        return None
    if data.get('pending') or data.get('queued_for_mac') or data.get('stale'):
        return None
    if leaf=='manage_events' and action not in ('shift','set','delete','create','update'):
        return None
    if leaf=='shopping_list' and action not in ('add','remove','check','uncheck','clear','update','set'):
        return None
    if leaf=='manage_reminders' and (action not in ('create','update','complete','reopen','delete') or not data.get('applied')):
        return None
    if leaf=='remind_me' and not data.get('applied'):
        return None
    if (leaf.startswith('personal_task_') or leaf=='manage_tasks') and (action or leaf.rsplit('_',1)[-1]) in ('list','get','status','search','result'):
        return None
    if leaf=='manage_plan' and action in ('','get'): return None
    card=None
    if leaf=='get_weather' and data.get('now'):
        now=data['now']
        metrics=[]
        for key,label,unit in [('temp_c','Now','°C'),('feels_c','Feels like','°C'),('wind_kmh','Wind',' km/h')]:
            if now.get(key) is not None: metrics.append({'label':label,'value':_text(now[key])+unit})
        series=[{'label':_text(x.get('time'),12),'value':x['temp_c']} for x in data.get('hourly',[])[:12]
                if isinstance(x,dict) and isinstance(x.get('temp_c'),(int,float))]
        rows=[_row(x.get('time'),f"{x.get('condition','')} · {x.get('rain_prob',0)}% rain") for x in data.get('hourly',[])[:6] if isinstance(x,dict)]
        card={'kind':'weather','title':data.get('place') or 'Weather','subtitle':now.get('condition') or '',
              'metrics':metrics,'series':series,'rows':rows}
    elif leaf in ('create_event','manage_events') and d is not None:
        events=[data['event']] if isinstance(data.get('event'),dict) else data.get('events',[])
        action=a.get('action') or ('create' if leaf=='create_event' else 'list')
        rows=[_row(x.get('summary') or x.get('title') or 'Event',
                   ' · '.join(_text(v) for v in [x.get('dtstart') or x.get('start'),x.get('location')] if v),
                   'queued' if data.get('queued_for_mac') else action) for x in events[:12] if isinstance(x,dict)]
        title='Calendar' if action in ('list','get','search') else 'Calendar updated'
        if data.get('queued_for_mac'):title='Calendar change queued'
        card={'kind':'calendar','title':title,'subtitle':f"{len(events)} event{'s' if len(events)!=1 else ''}", 'rows':rows}
    elif leaf=='shopping_list' and isinstance(data.get('items'),list):
        items=data['items'];left=sum(not x.get('checked') for x in items)
        card={'kind':'shopping','title':'Shopping list','subtitle':f'{left} to get · {len(items)-left} checked',
              'rows':[_row(x.get('text'),x.get('quantity'), 'done' if x.get('checked') else 'todo') for x in items[:20]]}
    elif leaf in ('remind_me','manage_reminders') and data.get('pending'):
        requested=data.get('requested') or {}
        card={'kind':'reminders','title':'Waiting for your iPhone','subtitle':'Not saved in Apple Reminders yet',
              'rows':[_row(requested.get('text') or 'Reminder change',requested.get('due_at') or requested.get('due_date') or '', 'pending')]}
    elif leaf in ('remind_me','manage_reminders') and isinstance(data.get('reminder'),dict):
        reminder=data['reminder'];action=data.get('action') or ('create' if leaf=='remind_me' else '')
        title={'create':'Reminder created','update':'Reminder updated','complete':'Reminder completed','reopen':'Reminder reopened'}.get(action,'Apple Reminder')
        card={'kind':'reminders','title':title,'subtitle':reminder.get('list_name') or 'Apple Reminders',
              'rows':[_row(reminder.get('text'),reminder.get('due_at') or reminder.get('due_date') or 'No due date',reminder.get('status') or 'pending')]}
    elif leaf=='remind_me' and data.get('due_at'):
        card={'kind':'reminders','title':'Reminder set','rows':[_row(data.get('text'),data['due_at'],'pending')]}
    elif leaf=='manage_reminders' and d is not None:
        rows=[_row(x.get('text'),x.get('due_at'),'pending') for x in data.get('reminders',[])[:16]]
        card={'kind':'reminders','title':'Reminder deleted' if data.get('deleted') else 'Apple Reminders' if data.get('source')=='apple_reminders' else 'Reminders','subtitle':'Cancelled' if data.get('cancelled') else f'{len(rows)} pending','rows':rows}
    elif leaf.startswith('personal_task_') or leaf=='manage_tasks':
        if d is None:return None
        tasks=[data['task']] if isinstance(data.get('task'),dict) else data.get('tasks',[])
        if isinstance(d,list):tasks=d
        rows=[_row(x.get('title') or x.get('name'),' · '.join(str(v) for v in [x.get('schedule'),x.get('scheduled_date'),x.get('scheduled_time')] if v),x.get('status')) for x in tasks[:16] if isinstance(x,dict)]
        card={'kind':'tasks','title':'Personal tasks' if leaf.startswith('personal') else 'Tasks',
              'subtitle':'Started' if data.get('started') else 'Deleted' if data.get('deleted') else '', 'rows':rows}
    elif leaf in ('manage_agents','coding_agents','agent_list_sessions','agent_inspect_session') and d is not None:
        agents=data.get('agents') or data.get('sessions') or ([data] if data.get('session_id') else [])
        if isinstance(d,list):agents=d
        rows=[_row(x.get('title') or x.get('name') or x.get('agent_type') or x.get('session_id'),
                   ' · '.join(str(v) for v in [x.get('host'),x.get('progress') or x.get('cwd')] if v),
                   x.get('status') or ('ready' if x.get('controllable') else '')) for x in agents[:12] if isinstance(x,dict)]
        card={'kind':'agents','title':'Coding agents' if leaf!='manage_agents' else 'Background agents','rows':rows,
              'subtitle':f"{len(data.get('host_errors') or [])} host(s) unavailable" if data.get('host_errors') else ''}
    elif leaf in ('muse_remember','muse_update') and data.get('ok'):
        card={'kind':'memory','title':'Memory updated' if leaf=='muse_update' else 'Remembered',
              'subtitle':a.get('file') or 'about-me','rows':[_row(data.get('fact') or a.get('fact'))]}
    elif leaf=='muse_forget' and data.get('ok'):
        card={'kind':'memory','title':'Memory removed','subtitle':f"{data.get('removed',0)} facts", 'rows':[]}
    elif leaf in ('muse_recall','manage_memory') and isinstance(data.get('matches'),list):
        card={'kind':'memory','title':'Saved memory','rows':[_row(x.get('text'),x.get('file')) for x in data['matches'][:8]]}
    elif leaf=='health_data' and data.get('metrics'):
        metrics=[]
        for key,value in data['metrics'].items():
            if isinstance(value,dict): value=value.get('average',value.get('avg',value.get('total')))
            if isinstance(value,(int,float)):
                metrics.append({'label':key.replace('_',' ').title(),'value':str(round(value,1))})
        card={'kind':'health','title':'Health summary','subtitle':f"{data.get('days_with_data',0)} days with data",'metrics':metrics[:6],'rows':[]}
    elif leaf=='manage_plan' and isinstance(data.get('steps'),list):
        card={'kind':'plan','title':'Plan','rows':[_row(x.get('text'),'',x.get('status') or 'todo') for x in data['steps'][:16]]}
    elif leaf in ('trigger_research','manage_research') and d is not None:
        reports=data.get('research') or data.get('reports') or ([data] if data.get('title') or data.get('id') else [])
        if isinstance(reports,dict):reports=[reports]
        card={'kind':'research','title':'Research','rows':[_row(x.get('title') or x.get('query') or 'Research',x.get('summary'),x.get('status')) for x in reports[:8] if isinstance(x,dict)]}
    elif leaf=='read_email' and data.get('uid') and data.get('subject'):

        emails=data.get('emails') or data.get('messages') or ([data] if data.get('subject') else [])
        card={'kind':'email','title':'Email found','rows':[_row(x.get('subject'),x.get('from_name') or x.get('from_address') or x.get('from') or x.get('sender')) for x in emails[:10] if isinstance(x,dict)]}
    elif data.get('applied') is True and isinstance(data.get('visualization'),dict):
        # Explicit plugin contract: data only; no HTML/JavaScript executes in a card.
        v=data['visualization']
        if v.get('kind') in KINDS:
            card={'kind':v['kind'],'title':v.get('title') or 'Update','subtitle':v.get('subtitle') or '',
                  'rows':[_row(x.get('title'),x.get('detail'),x.get('state')) for x in (v.get('rows') or [])[:16] if isinstance(x,dict)]}
    if card is None:return None
    if card['kind']!='plan' and not any(card.get(k) for k in ('rows','metrics','series')):return None
    card['id']=hashlib.sha256((name+json.dumps(a,sort_keys=True,default=str)+str(output)).encode()).hexdigest()[:20]
    entity=data.get('uid') or (data.get('reminder') or {}).get('id') or (data.get('task') or {}).get('id')
    if entity:card['id']=hashlib.sha256((card['kind']+':'+str(entity)).encode()).hexdigest()[:20]
    if card['kind']=='shopping':card['id']='shopping-list-result'
    if card['kind']=='plan': card['id']='working-plan'
    card['presentation']='candidate' if card['kind'] in ('weather','email','health','research') else 'outcome'
    card['title']=_text(card.get('title'),100)
    card['subtitle']=_text(card.get('subtitle'),180)
    for key in ('rows','metrics','series'):card.setdefault(key,[])
    return card


def select_result_visualizations(events, reply, user_message=''):
    """Publish confirmed changes, plus only the specific read result in the answer.

    This runs once after the answer, so intermediate searches/inspections cannot
    flash cards into chat. No model call or guessing success from prose.
    """
    text=(reply or '').casefold()
    intent=(user_message or '').casefold()
    stop={'the','and','your','from','with','this','that','about','email','mail','message','you','for','has','was','are'}
    selected={}
    for event in events:
        card=event.pop('_visualization_candidate',None)
        if not card: continue
        kind=card['kind']
        score=0
        if card.get('presentation')=='candidate':
            if kind=='email':
                subject=' '.join(row['title'] for row in card.get('rows',[])).casefold()
                words={w for w in re.findall(r"[\w]+",subject) if len(w)>2 and w not in stop}
                matches=sum(bool(re.search(r'\b'+re.escape(w)+r'\b',text)) for w in words)
                if not subject or not (subject in text or matches>=2): continue
                if re.search(r"could(?:n.t| not) find|unable to find|no matching",text): continue
                score=100 if subject in text else matches
            elif kind=='weather':
                if not re.search(r'weather|forecast|briefing|temperature|rain|cold|hot|sunny',intent): continue
                if not re.search(r'weather|forecast|rain|temperature|°',text): continue
            elif kind=='health':
                if not re.search(r'health|sleep|steps|heart|exercise|fitness',intent): continue
            else:
                # Polling research status is not the completed report.
                continue
        card['presentation']='outcome'
        key=kind if kind in ('email','weather','health') else card['id']
        prior=selected.get(key)
        if prior is None or score>=prior[0]: selected[key]=(score,event,card)
    results=[]
    for _,event,card in selected.values():
        event['visualization']=card
        results.append(card)
    return results
