import pandas as pd
import numpy as np
from math import sqrt
from gurobipy import *
import datetime as dt
from flask import jsonify, request

stocks = pd.DataFrame.from_csv('./static/data/stocks.csv')
etfs = pd.DataFrame.from_csv('./static/data/etfs.csv')
bonds = pd.DataFrame.from_csv('./static/data/bonds.csv')

# set default amount to $1m, default risk = 13% which corresponds to 0.5 of risk constant in MAD, max percent in a instrument = 5%
# transaction costs for stocks is $7 per unit
def getPortfolio(df,unused,amount = 1000000,risk = 13, maxP = 5):
    print("Using mean absolute deviation for risk calculations")
    T, k = df.shape
    vol = np.cov((df.iloc[1:, :] / df.shift(1).iloc[1:, :]).T) * df.shape[0]
    ret = (np.array(df.tail(1)) / np.array(df.head(1))).ravel()

    data = df.pct_change()[1:]
    periodicMeans = pd.groupby(data,by=[data.index.month]).mean()
    timePeriods = periodicMeans.index
    total_means = data.mean().values
    syms = data.columns
    m = Model("portfolio")
    portsyms =[]
    etfsyms= []
    bondsyms= []
    for sym in syms:
        if sym[:3] == 'ETF':
            etfsyms.append(sym)
        elif sym[:3] == 'BND':
            bondsyms.append(sym)
        else:
            portsyms.append(sym)
    portvars = [m.addVar(name=symb,lb=0.0) for symb in portsyms]
    portvars = pd.Series(portvars,index=portsyms)
    etfvars = [m.addVar(name=symb,lb=0.0) for symb in etfsyms]
    etfvars = pd.Series(etfvars,index=etfsyms)
    bondvars = [m.addVar(name=symb,lb=0.0) for symb in bondsyms]
    bondvars = pd.Series(bondvars,index=bondsyms)
    # Define new variables to implement Mean Absolute Deviation for risk calculation

    timevars = [m.addVar(name=str(t),lb=0.0) for t in timePeriods]
    timevars = pd.Series(timevars,index=timePeriods)

    allvars = pd.concat([portvars,etfvars,bondvars]).sort_index()
    portfolio = pd.DataFrame({'Variables': allvars})
    m.update()

    p_total_port = allvars.sum()
    p_return = total_means.dot(allvars)


    m.setObjective(p_return,GRB.MAXIMIZE)
    m.addConstr(p_total_port,GRB.EQUAL,amount)
    # max of 5% in etfs
    m.addConstr(etfvars.sum(),GRB.LESS_EQUAL,amount*0.05)
    # minimum of 10 % in bonds
    m.addConstr(bondvars.sum(),GRB.GREATER_EQUAL,amount*0.10)
    # MAD constraints
    w = risk

    m.addConstr(timevars.sum(),GRB.LESS_EQUAL,w*amount*0.01/2)
    for index, row in periodicMeans.iterrows():
        m.addConstr(timevars[index],GRB.GREATER_EQUAL,(row-total_means).dot(allvars))
    for var in allvars:
        m.addConstr(var,GRB.LESS_EQUAL,amount/100*maxP)


    m.update()
    m.optimize()

    portfolio['stocks'] = allvars.apply(lambda x:x.getAttr('x'))

    pos = portfolio['stocks'].as_matrix()
    pos[np.isclose(pos, 0, atol=1e-3)] = 0
    pos /= np.sum(pos)
    df["pos"] = df.dot(pos)
    perf = df["pos"].to_frame().reset_index()
    perf["index"] = perf["index"].map(lambda d: str(d.date()))
    perf.columns = ["date", "value"]
    allocation = { "L": [{"symbol": s, "p": 0} for s in unused], "S": [],
                   "min": perf["value"].min(),
                   "max": perf["value"].max(),
                   "series": perf.T.to_json() }

    category = lambda x : "S" if x < 0 else "L"

    for i, p in enumerate(pos):
        allocation[category(p)].append({ "symbol": df.columns[i], "p": p })

    allocation["ret"] = (ret.dot(pos) ** ( 365.0 / T ) - 1) * 100

    allocation["vol"] = np.sqrt(pos.dot(vol).dot(pos)) * np.sqrt( 365.0 / T ) * 100
    # allocation["status"] = sol["status"]
    allocation["status"] = "optimal"

    return jsonify(allocation)

def getPortfolioValue(portfolio,startDate="2016-01-01",endDate="2016-03-10"):
    data = stocks
    start = dt.datetime.strptime(startDate, "%Y-%m-%d").date()
    end = dt.datetime.strptime(endDate, "%Y-%m-%d").date()
    returnsData = data.pct_change()[1:]
    returns_for_dates = returnsData[start:end]
    ret_index = (1+returns_for_dates).cumprod()
    return int(ret_index.tail(1).dot(portfolio['stocks'].as_matrix()))

def getRebalance(df, freq, pos):
    reweight = df.groupby(pd.Grouper(freq=freq)).head(1)
    p = pos.copy()
    for idx, row in reweight.iterrows():
        p = pos / row * row.dot(p)
        reweight.loc[idx, :] = np.array(p)

    perf = df.groupby(pd.Grouper(freq=freq)).apply(lambda x: x.dot(reweight.loc[x.index[0], :])).to_frame()
    perf.index = perf.index.droplevel(0)
    perf = perf.reset_index()
    perf["index"] = perf["index"].map(lambda d: str(d.date()))
    perf.columns = ["date", "value"]
    dailyret = np.array(perf["value"])
    dailyret = dailyret[1:] / dailyret[:-1]

    return jsonify({ "min": perf["value"].min(),
                     "max": perf["value"].max(),
                     "ret": (perf["value"].iloc[-1] ** (365.0 / perf["value"].size) - 1) * 100,
                     "vol": np.std(dailyret) * 1910.5,
                     "series": perf.T.to_json() })

def rebalance(oldPortfolio,amount,risk = 13,expectedR = 0, maxP = 5, startDate = "2015-01-01", endDate = "2016-03-10"):
    start = dt.datetime.strptime(startDate, "%Y-%m-%d").date()
    end = dt.datetime.strptime(endDate, "%Y-%m-%d").date()
    dataR = stocks[start:end]
    closingPrices = dataR.tail(1)
    returnsData = dataR.pct_change()[1:]
    data = returnsData

    periodicMeans = pd.groupby(data,by=[data.index.month]).mean()
    timePeriods = periodicMeans.index
    total_means = data.mean().values
    syms = data.columns

    m = Model("rebalanced_portfolio")
    # variables defined for sell and buy below are changes in the amount of stocks

    portvarssell = [m.addVar(name=sym+"sell",lb=0.0) for sym in syms]
    portvarssell = pd.Series(portvarssell,index=syms+"sell")
    portvarsbuy = [m.addVar(name=sym +"buy",lb=0.0) for sym in syms]
    portvarsbuy = pd.Series(portvarsbuy,index=syms+"buy")
    portvarsnochange = [m.addVar(name=sym +"buy",lb=0.0) for sym in syms]
    portvarsnochange = pd.Series(portvarsnochange,index=syms+"buy")


    #BINARIES FOR BUY AND SELL
    binaryvarssell = [m.addVar(name=symb+"binary_sell",vtype=GRB.BINARY) for symb in syms]
    binaryvarssell= pd.Series(binaryvarssell, index=syms+"binary_sell")
    binaryvarsbuy = [m.addVar(name=symb+"binary_buy",vtype=GRB.BINARY) for symb in syms]
    binaryvarsbuy= pd.Series(binaryvarsbuy, index=syms+"binary_buy")

    # Define new variables to implement Mean Absolute Deviation
    timevars = [m.addVar(name=str(t),lb=0.0) for t in timePeriods]
    timevars = pd.Series(timevars,index=timePeriods)

    allvars = pd.concat([portvarssell,portvarsbuy,portvarsnochange, binaryvarssell, binaryvarsbuy])
    portfolio = pd.DataFrame({'Variables': allvars})
    m.update()

    MoneySpentOnBuy = portvarsbuy.values - oldPortfolio['stocks'].values * binaryvarsbuy.values
    noOfUnitsBought =  (MoneySpentOnBuy / closingPrices.values).transpose()
    MoneySpentOnSell  = oldPortfolio['stocks'].values * binaryvarssell.values - portvarssell.values
    noOfUnitsSold = (MoneySpentOnSell / closingPrices.values).transpose()
    sell_transactionCost = 7*np.ones(binaryvarssell.shape).dot(noOfUnitsBought)
    buy_transactionCost = 7*np.ones(binaryvarssell.shape).dot(noOfUnitsSold)
    transactionCost = sell_transactionCost.sum() + buy_transactionCost.sum()
    m.update()

    finalValues = portvarsbuy.values + portvarssell.values + portvarsnochange.values

    p_total_port = finalValues.sum()
    p_return = total_means.dot(finalValues)

    for i, var in enumerate(binaryvarssell):
        m.addConstr(var+binaryvarsbuy[i],GRB.LESS_EQUAL,1)

    for i, var in enumerate(portvarssell):
        m.addConstr(var,GRB.LESS_EQUAL,amount*maxP/100)

    for i, var in enumerate(portvarsbuy):
        m.addConstr(var,GRB.LESS_EQUAL,amount*maxP/100)

    for i, var in enumerate(portvarsnochange):
        m.addConstr(var,GRB.LESS_EQUAL,amount*maxP/100)


    m.setObjective(transactionCost,GRB.MINIMIZE)

    m.addConstr(p_total_port,GRB.EQUAL,amount)

    # MAD constraints
    m.addConstr(timevars.sum(),GRB.LESS_EQUAL,amount*maxP*0.01/2)

    for index, row in periodicMeans.iterrows():
        m.addConstr(timevars[index],GRB.GREATER_EQUAL,(row-total_means).dot(finalValues))


    m.addConstr(p_return,GRB.GREATER_EQUAL,expectedR)


    m.optimize()
    #Fix the budget

    # portfolio['new Model'] = allvars.apply(lambda x:x.getAttr('x'))
    portfolio['stocks'] = allvars.apply(lambda x:x.getAttr('x'))

    return portfolio

def getFrontier(df, short):
    T, k = df.shape

    vol = np.cov((df.iloc[1:, :] / df.shift(1).iloc[1:, :]).T) * df.shape[0]
    ret = (np.array(df.tail(1)) / np.array(df.head(1))).ravel()

    sigma = df.cov()
    stats = pd.concat((df.mean(),df.std(),(df+1).prod()-1),axis=1)
    stats.columns = ['Mean_return', 'Volatility', 'Total_return']

    extremes = pd.concat((stats.idxmin(),stats.min(),stats.idxmax(),stats.max()),axis=1)
    extremes.columns = ['Minimizer','Minimum','Maximizer','Maximum']
    growth = (df+1.0).cumprod()
    tx = growth.index
    syms = growth.columns
    # Instantiate our model
    m = Model("portfolio")

    m.setParam('OutputFlag',False)

    # Create one variable for each stock
    portvars = [m.addVar(name=symb,lb=0.0) for symb in syms]
    portvars = pd.Series(portvars, index=syms)
    portfolio = pd.DataFrame({'Variables':portvars})
    m.update()

    # The total budget
    p_total = portvars.sum()

    # The mean return for the portfolio
    p_return = stats['Mean_return'].dot(portvars)

    # The (squared) volatility of the portfolio
    p_risk = sigma.dot(portvars).dot(portvars)

    m.setObjective(p_risk,GRB.MINIMIZE)

    # Fix the budget
    m.addConstr(p_total, GRB.EQUAL, 1)

    m.setParam('Method',1)

    frontier = {}
    fixedreturn = m.addConstr(p_return, GRB.EQUAL, 10)
    m.update()

    # Determine the range of returns. Make sure to include the lowest-risk
    # portfolio in the list of options
    minret = extremes.loc['Mean_return','Minimum']
    maxret = extremes.loc['Mean_return','Maximum']
    riskret = extremes.loc['Volatility','Minimizer']
    riskret = stats.loc[riskret,'Mean_return']
    returns = np.unique(np.hstack((np.linspace(minret,maxret,100),riskret)))

    # Iterate through all returns
    risks = returns.copy()
    for i, alpha in enumerate(returns):
        fixedreturn.rhs = returns[i]
        m.optimize()
        pos = portvars.apply(lambda x:x.getAttr('x')).as_matrix()
        frontier[i] = { "ret": returns[i],
                        "vol": sqrt(p_risk.getValue())}
    return jsonify(frontier)

def getData():
    raw = pd.read_json(request.form["data"])
    symbols = list(raw.columns)
    data = pd.DataFrame(columns=symbols)
    for symbol in raw.columns:
        idx, val = zip(*list(map(lambda d: (dt.datetime.strptime(d["date"][:10], "%Y-%m-%d").date(), d["value"]), raw[symbol])))
        data[symbol] = pd.Series(data=val, index=pd.DatetimeIndex(idx))
    return data

def pullDataFromYahoo(symbol, startdate, enddate):
    dates = pd.DatetimeIndex(start=startdate, end=enddate, freq='1d')
    data = pd.DataFrame(index=dates)
    try:
        concatIns = pd.concat([stocks,etfs,bonds],axis=1)
        tmp = concatIns[symbol][startdate:enddate]
        tmp = tmp.to_frame()
        data["value"] = tmp[symbol]
        data = data.interpolate().ffill().bfill()
        data["value"] /= data["value"][0]
        data = data.reset_index()
        data["date"] = data["index"].apply(lambda d: str(d.date()))
        dailyret = np.array(data["value"])
        dailyret = dailyret[1:] / dailyret[:-1]
        return jsonify({ "series": data.drop("index", 1).T.to_json(),
                         "ret": (data["value"].iloc[-1] ** ( 365.0 / data["value"].size ) - 1) * 100,
                         "vol": np.std(dailyret) * 1910.5 })

    except:
        return "invalid"