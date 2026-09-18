-- HR Support Agent — Supabase/Postgres schema + seed.
-- Run once against a fresh project (SQL editor). Tables are prefixed hr_ and live in
-- public so PostgREST exposes them. Access is via the anon key with permissive RLS
-- policies (the API is the only client). hr_reset() reseeds; hr_apply_leave() is the
-- atomic write path used by the apply_leave tool.

create table if not exists public.hr_employees (
  employee_id text primary key,
  name text not null,
  email text not null,
  department text not null,
  designation text not null,
  location text not null,
  employment_type text not null check (employment_type in ('permanent','contractor','intern')),
  status text not null check (status in ('active','probation','notice_period','on_leave','inactive')),
  join_date date not null,
  probation_end_date date,
  manager_id text,
  resignation_date date,
  last_working_day date
);

create table if not exists public.hr_leave_balances (
  employee_id text not null references public.hr_employees(employee_id) on delete cascade,
  leave_type text not null check (leave_type in ('CL','SL','PL','COMP_OFF','LOP')),
  entitled numeric(5,1) not null default 0,
  carried_forward numeric(5,1) not null default 0,
  used numeric(5,1) not null default 0,
  available numeric(5,1) not null default 0,
  expires_on date,
  primary key (employee_id, leave_type)
);

create sequence if not exists public.hr_request_seq start 1001;

create table if not exists public.hr_leave_requests (
  request_id text primary key,
  employee_id text not null references public.hr_employees(employee_id) on delete cascade,
  leave_type text not null,
  start_date date not null,
  end_date date not null,
  days numeric(5,1) not null,
  half_day boolean not null default false,
  status text not null check (status in ('PENDING_APPROVAL','APPROVED','REJECTED','CANCELLED')),
  reason text,
  approver_id text,
  applied_at timestamptz not null default now(),
  source text not null default 'agent'
);

create table if not exists public.hr_holidays (
  id bigserial primary key,
  holiday_date date not null,
  name text not null,
  holiday_type text not null check (holiday_type in ('national','regional','optional','shutdown')),
  location text
);

create table if not exists public.hr_sessions (
  session_id uuid primary key default gen_random_uuid(),
  employee_id text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.hr_messages (
  id bigserial primary key,
  session_id uuid not null references public.hr_sessions(session_id) on delete cascade,
  role text not null,
  content jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists hr_messages_session_idx on public.hr_messages(session_id, id);

create table if not exists public.hr_audit_log (
  id bigserial primary key,
  session_id uuid,
  employee_id text,
  tool text not null,
  args jsonb,
  result jsonb,
  ok boolean not null,
  latency_ms integer,
  chaos text,
  created_at timestamptz not null default now()
);

alter table public.hr_employees enable row level security;
alter table public.hr_leave_balances enable row level security;
alter table public.hr_leave_requests enable row level security;
alter table public.hr_holidays enable row level security;
alter table public.hr_sessions enable row level security;
alter table public.hr_messages enable row level security;
alter table public.hr_audit_log enable row level security;

do $$
declare t text;
begin
  foreach t in array array['hr_employees','hr_leave_balances','hr_leave_requests','hr_holidays','hr_sessions','hr_messages','hr_audit_log'] loop
    execute format('drop policy if exists hr_anon_all on public.%I', t);
    execute format('create policy hr_anon_all on public.%I for all to anon using (true) with check (true)', t);
  end loop;
end $$;

create or replace function public.hr_reset(clear_sessions boolean default false)
returns jsonb
language plpgsql
security definer
as $$
begin
  delete from public.hr_leave_requests;
  delete from public.hr_leave_balances;
  delete from public.hr_holidays;
  delete from public.hr_employees;
  delete from public.hr_audit_log;
  if clear_sessions then
    delete from public.hr_messages;
    delete from public.hr_sessions;
  end if;
  perform setval('public.hr_request_seq', 1001, false);

  insert into public.hr_employees (employee_id,name,email,department,designation,location,employment_type,status,join_date,probation_end_date,manager_id,resignation_date,last_working_day) values
  ('E1001','Ananya Rao','ananya.rao@nimbusdyn.example','Engineering','Senior Software Engineer','Hyderabad','permanent','active','2022-03-14',null,'E1006',null,null),
  ('E1002','Rahul Verma','rahul.verma@nimbusdyn.example','Finance','Financial Analyst','Hyderabad','permanent','active','2021-08-02',null,'E1010',null,null),
  ('E1003','Priya Nair','priya.nair@nimbusdyn.example','Engineering','Software Engineer','Hyderabad','permanent','probation','2026-07-01','2026-12-31','E1006',null,null),
  ('E1004','Vikram Singh','vikram.singh@nimbusdyn.example','Sales','Regional Sales Manager','Mumbai','permanent','active','2018-01-15',null,'E1011',null,null),
  ('E1005','Meera Iyer','meera.iyer@nimbusdyn.example','Product','Product Manager','Bengaluru','permanent','on_leave','2020-11-09',null,'E1011',null,null),
  ('E1006','Arjun Mehta','arjun.mehta@nimbusdyn.example','Engineering','Engineering Manager','Hyderabad','permanent','active','2017-06-05',null,'E1010',null,null),
  ('E1007','Sneha Kulkarni','sneha.kulkarni@nimbusdyn.example','Marketing','Content Lead','Bengaluru','permanent','notice_period','2023-02-20',null,'E1011','2026-09-01','2026-10-30'),
  ('E1008','Karthik Reddy','karthik.reddy@nimbusdyn.example','IT Operations','DevOps Consultant','Bengaluru','contractor','active','2026-02-01',null,'E1010',null,null),
  ('E1009','David Thomas','david.thomas@nimbusdyn.example','Sales','Account Executive','Mumbai','permanent','inactive','2019-09-23',null,'E1011','2026-05-15','2026-06-30'),
  ('E1010','Lakshmi Menon','lakshmi.menon@nimbusdyn.example','Executive','Chief Operating Officer','Hyderabad','permanent','active','2016-04-01',null,null,null,null),
  ('E1011','Rohan Desai','rohan.desai@nimbusdyn.example','Sales','VP Sales & Product','Mumbai','permanent','active','2016-09-12',null,'E1010',null,null),
  ('E1012','Fatima Sheikh','fatima.sheikh@nimbusdyn.example','Engineering','Intern','Hyderabad','intern','active','2026-08-03','2027-01-31','E1006',null,null);

  insert into public.hr_leave_balances (employee_id,leave_type,entitled,carried_forward,used,available,expires_on) values
  ('E1001','CL',12,0,4,8,'2026-12-31'), ('E1001','SL',10,0,3,7,null), ('E1001','PL',18,6,10,14,null), ('E1001','COMP_OFF',0,0,0,2,'2026-10-05'),
  ('E1002','CL',12,0,12,0,'2026-12-31'), ('E1002','SL',10,0,8,2,null), ('E1002','PL',18,0,16,2,null), ('E1002','COMP_OFF',0,0,0,0,null),
  ('E1003','CL',6,0,1,3,'2026-12-31'), ('E1003','SL',5,0,0,5,null), ('E1003','PL',0,0,0,4.5,null), ('E1003','COMP_OFF',0,0,0,0,null),
  ('E1004','CL',12,0,2,10,'2026-12-31'), ('E1004','SL',10,0,0,10,null), ('E1004','PL',18,30,18,30,null), ('E1004','COMP_OFF',0,0,0,1,'2026-09-20'),
  ('E1005','CL',12,0,0,12,'2026-12-31'), ('E1005','SL',10,0,0,10,null), ('E1005','PL',18,12,0,30,null), ('E1005','COMP_OFF',0,0,0,0,null),
  ('E1006','CL',12,0,5,7,'2026-12-31'), ('E1006','SL',10,0,2,8,null), ('E1006','PL',18,20,6,32,null), ('E1006','COMP_OFF',0,0,0,0,null),
  ('E1007','CL',12,0,9,3,'2026-12-31'), ('E1007','SL',10,0,4,6,null), ('E1007','PL',18,4,10,12,null), ('E1007','COMP_OFF',0,0,0,0,null),
  ('E1008','LOP',0,0,3,0,null),
  ('E1009','CL',12,0,2,10,'2026-12-31'), ('E1009','SL',10,0,0,10,null), ('E1009','PL',18,0,5,13,null),
  ('E1010','CL',12,0,3,9,'2026-12-31'), ('E1010','SL',10,0,0,10,null), ('E1010','PL',18,30,4,44,null),
  ('E1011','CL',12,0,6,6,'2026-12-31'), ('E1011','SL',10,0,1,9,null), ('E1011','PL',18,30,12,36,null),
  ('E1012','CL',6,0,0,1.5,'2026-12-31'), ('E1012','SL',5,0,0,1,null);

  insert into public.hr_leave_requests (request_id,employee_id,leave_type,start_date,end_date,days,half_day,status,reason,approver_id,applied_at,source) values
  ('LR-2026-0871','E1001','PL','2026-08-10','2026-08-14',5,false,'APPROVED','Family trip','E1006','2026-07-28 10:12:00+05:30','portal'),
  ('LR-2026-0902','E1006','CL','2026-09-25','2026-09-25',1,false,'PENDING_APPROVAL','Personal work','E1010','2026-09-15 09:40:00+05:30','portal'),
  ('LR-2026-0915','E1004','PL','2026-10-19','2026-10-23',5,false,'PENDING_APPROVAL','Diwali travel','E1011','2026-09-16 17:05:00+05:30','portal'),
  ('LR-2026-0920','E1005','PL','2026-08-01','2027-01-29',130,false,'APPROVED','Maternity leave (recorded under PL bucket by legacy system)','E1011','2026-07-10 11:00:00+05:30','portal'),
  ('LR-2026-0933','E1002','SL','2026-09-16','2026-09-17',2,false,'APPROVED','Fever','E1010','2026-09-16 08:30:00+05:30','portal');

  insert into public.hr_holidays (holiday_date,name,holiday_type,location) values
  ('2026-01-01','New Year''s Day','national',null),
  ('2026-01-14','Makara Sankranti / Pongal','national',null),
  ('2026-01-26','Republic Day','national',null),
  ('2026-03-04','Holi','national',null),
  ('2026-03-19','Ugadi / Gudi Padwa','national',null),
  ('2026-04-03','Good Friday','national',null),
  ('2026-05-01','May Day','national',null),
  ('2026-08-15','Independence Day','national',null),
  ('2026-09-14','Ganesh Chaturthi','regional','Mumbai'),
  ('2026-09-21','Bathukamma','regional','Hyderabad'),
  ('2026-10-02','Gandhi Jayanti','national',null),
  ('2026-10-20','Dussehra / Vijaya Dashami','national',null),
  ('2026-11-01','Kannada Rajyotsava','regional','Bengaluru'),
  ('2026-11-09','Diwali (observed)','national',null),
  ('2026-11-10','Diwali - Govardhan Puja','regional','Mumbai'),
  ('2026-12-25','Christmas','national',null),
  ('2026-12-28','Year-end shutdown','shutdown',null),
  ('2026-12-29','Year-end shutdown','shutdown',null),
  ('2026-12-30','Year-end shutdown','shutdown',null),
  ('2026-12-31','Year-end shutdown','shutdown',null),
  ('2026-02-14','Maha Shivaratri','optional',null),
  ('2026-03-31','Eid ul-Fitr','optional',null),
  ('2026-04-14','Ambedkar Jayanti','optional',null),
  ('2026-08-28','Onam','optional',null),
  ('2026-11-04','Guru Nanak Jayanti','optional',null);

  return jsonb_build_object('ok', true, 'employees', (select count(*) from public.hr_employees), 'holidays', (select count(*) from public.hr_holidays), 'reset_at', now());
end;
$$;

create or replace function public.hr_apply_leave(p_employee_id text, p_leave_type text, p_start date, p_end date, p_days numeric, p_half_day boolean, p_reason text, p_approver text)
returns jsonb
language plpgsql
security definer
as $$
declare v_avail numeric; v_id text; v_dup text;
begin
  select request_id into v_dup from public.hr_leave_requests
    where employee_id = p_employee_id and status in ('PENDING_APPROVAL','APPROVED')
      and start_date <= p_end and end_date >= p_start limit 1;
  if v_dup is not null then
    return jsonb_build_object('ok', false, 'error_code', 'OVERLAPPING_REQUEST', 'existing_request_id', v_dup);
  end if;
  if p_leave_type <> 'LOP' then
    select available into v_avail from public.hr_leave_balances where employee_id = p_employee_id and leave_type = p_leave_type for update;
    if v_avail is null then
      return jsonb_build_object('ok', false, 'error_code', 'LEAVE_TYPE_NOT_ELIGIBLE');
    end if;
    if v_avail < p_days then
      return jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_BALANCE', 'available', v_avail, 'requested', p_days);
    end if;
    update public.hr_leave_balances set used = used + p_days, available = available - p_days
      where employee_id = p_employee_id and leave_type = p_leave_type;
  end if;
  v_id := 'LR-2026-' || lpad(nextval('public.hr_request_seq')::text, 4, '0');
  insert into public.hr_leave_requests (request_id,employee_id,leave_type,start_date,end_date,days,half_day,status,reason,approver_id,source)
    values (v_id, p_employee_id, p_leave_type, p_start, p_end, p_days, p_half_day, 'PENDING_APPROVAL', p_reason, p_approver, 'agent');
  return jsonb_build_object('ok', true, 'request_id', v_id, 'status', 'PENDING_APPROVAL', 'days_deducted', p_days, 'approver_id', p_approver,
    'remaining_balance', (select available from public.hr_leave_balances where employee_id = p_employee_id and leave_type = p_leave_type));
end;
$$;

grant execute on function public.hr_reset(boolean) to anon;
grant execute on function public.hr_apply_leave(text,text,date,date,numeric,boolean,text,text) to anon;

select public.hr_reset(true);
