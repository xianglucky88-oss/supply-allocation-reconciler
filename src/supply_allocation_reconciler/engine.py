def calculate(rows, payload):
    available=sum(float(x["qty"]) for x in payload.get("inventory", [])); allocations=[]; unmet=[]
    for demand in sorted(rows,key=lambda x:(x["priority"],x["order"])):
        allocated=min(available,float(demand["qty"])); available-=allocated; allocations.append({"order":demand["order"],"allocated":allocated,"requested":demand["qty"]})
        if allocated < float(demand["qty"]): unmet.append({"order":demand["order"],"unmet":float(demand["qty"])-allocated})
    return {"allocations":allocations,"unmet":unmet,"remaining_inventory":available}
