/* Real UI smoke tests. Set PLAYWRIGHT_MODULE or install playwright locally. */
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.FAST_PARSER_URL||'http://127.0.0.1:8000';
async function main(){
  const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'msedge'});
  const page=await browser.newPage({viewport:{width:1280,height:720}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    const firstScreens=[];
    for(let i=0;i<5;i++){const start=performance.now();await page.goto(base);await page.waitForSelector('#rows tr');firstScreens.push(performance.now()-start);}
    assert((await page.locator('#rows tr').count())>0);
    await page.locator('#rows tr').first().click();
    assert((await page.locator('#detail h2').textContent()).includes(' — '));
    assert((await page.locator('#detail').textContent()).includes('Источник'));
    await page.locator('#country').selectOption('Международные');
    assert.equal(await page.locator('#match').isDisabled(),true);
    const comps=(await (await page.request.get(base+'/api/v1/competitions?country='+encodeURIComponent('Международные'))).json()).items;
    const target=comps.find(c=>c.id.startsWith('sky:')&&c.state==='verified');
    assert(target,'Need verified international HTML matches for real-data smoke');
    const leagueTimes=[];
    for(let i=0;i<100;i++){
      await page.locator('#league').selectOption('');
      const start=performance.now();
      await Promise.all([page.waitForResponse(r=>r.url().includes('/api/v1/matches?')&&r.status()===200),page.locator('#league').selectOption(target.id)]);
      await page.waitForSelector('#rows tr');leagueTimes.push(performance.now()-start);
    }
    await page.locator('#rows tr').first().click();
    await page.locator('#settings-open').click();
    await page.waitForSelector('#settings-dialog[open]');
    const original=await (await page.request.get(base+'/api/v1/settings')).json();
    await page.locator('[name=timezone]').fill('invalid/timezone');
    await page.locator('#settings-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#settings-message').textContent.includes('пояс'));
    await page.locator('[name=timezone]').fill(original.timezone);
    await page.locator('#settings-form button[type=submit]').click();
    await page.waitForFunction(()=>!document.querySelector('#settings-dialog').open);
    // Invalid configuration must not leak into persistent storage.
    assert.equal((await (await page.request.get(base+'/api/v1/settings')).json()).timezone,original.timezone);
    // Coverage labels are operator evidence, independent of collection success.
    const selectedCid=await page.locator('#league').inputValue();
    const beforeVerification=(await (await page.request.get(base+'/api/v1/competitions')).json()).items.find(c=>c.id===selectedCid).coverage_verification;
    try{
      await page.locator('#verification-open').click();
      await page.locator('#verification-form [name=status]').selectOption('verified');
      await page.locator('#verification-form [name=scope]').fill('');
      await page.locator('#verification-form button[type=submit]').click();
      await page.waitForFunction(()=>document.querySelector('#verification-message').textContent.length>0);
      await page.locator('#verification-form [name=status]').selectOption('partial');
      await page.locator('#verification-form [name=fixtures]').check();
      await page.locator('#verification-form [name=results]').uncheck();
      await page.locator('#verification-form [name=live]').uncheck();
      await page.locator('#verification-form [name=scope]').fill('Synthetic UI test; not a real verification');
      await page.locator('#verification-form [name=evidence]').fill('Temporary automated UI test');
      await page.locator('#verification-form button[type=submit]').click();
      await page.waitForFunction(()=>!document.querySelector('#verification-dialog').open);
      assert((await page.locator('#coverage-status').textContent()).includes('Частично'));
      await page.reload();await page.waitForSelector('#rows tr');
      assert((await page.locator('#coverage-status').textContent()).includes('Частично'));
    }finally{
      const token=await page.locator('meta[name=csrf-token]').getAttribute('content');
      const restored=await page.request.put(base+'/api/v1/competitions/'+encodeURIComponent(selectedCid)+'/verification',{data:beforeVerification,headers:{Origin:base,'X-CSRF-Token':token}});
      assert.equal(restored.status(),200);
      await page.reload();await page.waitForSelector('#rows tr');
    }
    const dir=path.join(process.cwd(),'data','qa');fs.mkdirSync(dir,{recursive:true});
    await page.screenshot({path:path.join(dir,'desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'Mobile page overflows viewport');
    await page.screenshot({path:path.join(dir,'mobile.png'),fullPage:true});
    // Refresh preserves loaded pages and exposes stale data without unsafe HTML.
    let pageTemplate;
    await page.route('**/api/v1/matches?*',async route=>{
      const second=new URL(route.request().url()).searchParams.has('cursor');
      if(!second){const res=await route.fetch();pageTemplate=(await res.json()).items[0];}
      assert(pageTemplate);
      const body={items:Array.from({length:second?50:100},(_,i)=>({...pageTemplate,id:'ui-page-'+(i+(second?100:0)),is_stale:true})),next_cursor:second?null:'synthetic-page-2'};
      await route.fulfill({status:200,contentType:'application/json',json:body});
    });
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===100);
    await page.locator('#more').click();
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===150);
    await Promise.all([page.waitForResponse(r=>r.url().includes('cursor=synthetic-page-2')),page.locator('#refresh').click()]);
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===150);
    await page.locator('#rows tr').first().click();
    assert((await page.locator('#detail').textContent()).includes('Актуальность'));
    await page.unrouteAll({behavior:'wait'});
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length>0&&document.querySelectorAll('#rows tr').length<100);
    // Safety: render untrusted team text as textContent, not executable markup.
    await page.route('**/api/v1/matches?*',async route=>{
      const res=await route.fetch(),body=await res.json();
      if(body.items.length)body.items[0].home_team.name='<img src=x onerror="window.testXss=1">';
      await route.fulfill({response:res,json:body});
    });
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#rows').textContent.includes('<img'));
    assert.equal(await page.evaluate(()=>window.testXss),undefined);
    assert.equal(await page.locator('#rows img').count(),0);
    await page.unrouteAll({behavior:'wait'});
    // Browser must hide expired records even when the backend subsequently goes offline.
    await page.route('**/api/v1/matches?*',async route=>{
      const res=await route.fetch(),body=await res.json();
      for(const m of body.items)m.expires_at=new Date(Date.now()+1500).toISOString();
      await route.fulfill({response:res,json:body});
    });
    await Promise.all([page.waitForResponse(r=>r.url().includes('/api/v1/matches?')&&r.status()===200),page.locator('#refresh').click()]);
    await page.waitForSelector('#rows tr');
    await page.unrouteAll({behavior:'wait'});
    await page.route('**/api/v1/**',route=>route.abort());
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===0,{timeout:10000});
    assert.equal(await page.locator('#detail h2').textContent(),'Выберите матч');
    await page.unroute('**/api/v1/**');
    assert.deepEqual(errors,[]);
    const sorted=leagueTimes.sort((a,b)=>a-b);
    const result={browser:browser.version(),viewport:'1280x720;390x844',first_screen_ms:firstScreens,
      league_switches:100,league_switch_p95_ms:sorted[94],page_errors:errors,
      assertions:['real countries/leagues/matches','team names and card','invalid/valid settings','100 league switches','mobile overflow','XSS text rendering','expiry while backend offline','refresh preserves loaded pages','freshness card','coverage labels and validation','coverage persistence across reload']};
    fs.writeFileSync(path.join(dir,'browser-report.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
