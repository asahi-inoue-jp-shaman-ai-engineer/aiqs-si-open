import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createUnifiedEntry } from '../src/unified-entry.js';
const id='11111111-1111-4111-8111-111111111111';
const input={email:'synthetic@example.test',password:'synthetic',referralCode:'frozen'};
const deferred=()=>{let resolve; const promise=new Promise(r=>{resolve=r;}); return {promise,resolve};};
function fixture(factors=[]) {
 const clients=[],calls=[],sent=[];
 let request=async()=>({ok:true,json:async()=>({registration_complete:true})});
 const entry=createUnifiedEntry({origin:'https://authority.example',fetch:(...args)=>{calls.push(['complete',...args]);return request(...args);},handoff:async token=>{sent.push(token);},createClient:()=>{
  let session={access_token:'initial-token',user:{id}}; const user={id,factors};
  const result=()=>({data:{user,session}});
  const auth={signUp:async value=>{calls.push(['signup',value]);return {data:{user}};},
   signInWithPassword:async value=>{calls.push(['login',value]);return result();},
   signInWithOtp:async value=>{calls.push(['otp-request',value]);return {data:{}};},
   resetPasswordForEmail:async value=>{calls.push(['recover',value]);return {data:{}};},
   verifyOtp:async value=>{calls.push(['otp',value]);return result();},
   getSession:async()=>({data:{session}}),getUser:async()=>({data:{user}}),
   updateUser:async()=>{calls.push(['update']);return {data:{user}};},
   mfa:{challenge:async()=>({data:{id:'challenge',type:'totp',expires_at:Date.now()/1000+60}}),
    verify:async()=>{session={...session,access_token:'rotated-token'};return {data:{user,access_token:session.access_token}};}}};
  clients.push(auth);return {auth};
 }});
 return {entry,clients,calls,sent,setRequest:value=>{request=value;}};
}
const factor={id:'totp-1',factor_type:'totp',status:'verified'};
test('initial selection creates fresh exclusive clients without operations',async()=>{
 const f=fixture();f.entry.selectMode('signup');f.entry.selectMode('recovery');
 assert.equal(new Set(f.clients).size,3);assert.deepEqual(f.calls,[]);
 await assert.rejects(f.entry.invoke('signup',input));await assert.rejects(f.entry.invoke('passwordLogin',input));
 assert(f.entry.canSwitch);assert.throws(()=>f.entry.selectMode('unknown'));
});
test('login is email OTP only and hands off once without completion',async()=>{
 const f=fixture();
 await assert.rejects(f.entry.invoke('passwordLogin',input));
 assert(!f.calls.some(c=>c[0]==='login'));
 assert.equal((await f.entry.invoke('requestOtpLogin',input.email)).kind,'otp-code-required');
 assert.equal(f.entry.state,'otp-code');
 assert.deepEqual(f.calls.find(c=>c[0]==='otp-request')[1],{email:input.email,options:{shouldCreateUser:false}});
 await f.entry.invoke('verifyOtpLogin','123456');
 assert.equal(f.entry.state,'finished');assert.deepEqual(f.sent,['initial-token']);
 assert.equal(f.calls.find(c=>c[0]==='otp')[1].type,'email');
 assert(!f.calls.some(c=>['signup','complete','update'].includes(c[0])));
 await assert.rejects(f.entry.invoke('requestOtpLogin',input.email));assert.throws(()=>f.entry.selectMode('signup'));
});
test('signup freezes referral and explicitly retries completion only',async()=>{
 const f=fixture();f.entry.selectMode('signup');const supplied={...input};
 await f.entry.invoke('signup',supplied);supplied.referralCode='changed';assert.throws(()=>f.entry.selectMode('login'));
 await f.entry.invoke('verify','123456');f.setRequest(async()=>{throw Error();});
 await assert.rejects(f.entry.invoke('complete'));assert.equal(f.entry.state,'completion');
 await assert.rejects(f.entry.invoke('signup',input));f.setRequest(async()=>({ok:true,json:async()=>({registration_complete:true})}));
 await f.entry.invoke('complete');assert.deepEqual(f.sent,['initial-token']);
 assert.equal(f.calls.filter(c=>c[0]==='signup').length,1);assert.equal(f.calls.filter(c=>c[0]==='otp').length,1);
 assert.equal(f.calls.find(c=>c[0]==='signup')[1].options.data.referral_code,'frozen');
});
test('signup ambiguity cannot restart or switch',async()=>{
 const f=fixture();f.entry.selectMode('signup');f.clients.at(-1).signUp=async()=>{throw Error();};
 await assert.rejects(f.entry.invoke('signup',input));assert.equal(f.entry.state,'signup-pending');
 assert.throws(()=>f.entry.selectMode('login'));await assert.rejects(f.entry.invoke('signup',input));
});
test('login TOTP uses rotated token with no signup completion',async()=>{
 const f=fixture([factor]);await f.entry.invoke('requestOtpLogin',input.email);
 assert.equal((await f.entry.invoke('verifyOtpLogin','123456')).kind,'choose-totp');
 assert.deepEqual(f.sent,[]);await f.entry.invoke('chooseAndChallengeTOTP','totp-1');await f.entry.invoke('verifyTOTP','123456');
 assert.deepEqual(f.sent,['rotated-token']);assert(!f.calls.some(c=>c[0]==='complete'));
});
test('recovery waits for password update after OTP and TOTP',async()=>{
 const f=fixture([factor]);f.entry.selectMode('recovery');await f.entry.invoke('requestRecovery',input.email);
 await f.entry.invoke('verifyRecovery','123456');await f.entry.invoke('chooseAndChallengeTOTP','totp-1');await f.entry.invoke('verifyTOTP','123456');
 assert.equal(f.entry.state,'password-ready');assert.deepEqual(f.sent,[]);await f.entry.invoke('changePassword','new-synthetic');
 assert.deepEqual(f.sent,['rotated-token']);assert.equal(f.calls.find(c=>c[0]==='otp')[1].type,'recovery');
 assert(!f.calls.some(c=>['signup','complete'].includes(c[0])));
});
test('ambiguous update cannot resend, switch or hand off',async()=>{
 const f=fixture();f.entry.selectMode('recovery');await f.entry.invoke('requestRecovery',input.email);await f.entry.invoke('verifyRecovery','123456');
 let count=0;f.clients.at(-1).updateUser=async()=>{count++;throw Error();};
 await assert.rejects(f.entry.invoke('changePassword','new-synthetic'));assert.equal(f.entry.state,'password-update-unknown');
 await assert.rejects(f.entry.invoke('changePassword','again'));assert.equal(count,1);assert.throws(()=>f.entry.selectMode('login'));assert.deepEqual(f.sent,[]);
});
test('single flight rejects duplicate and switching while pending',async()=>{
 const f=fixture(),gate=deferred();let count=0;f.clients[0].signInWithOtp=()=>{count++;return gate.promise;};
 const pending=f.entry.invoke('requestOtpLogin',input.email);await assert.rejects(f.entry.invoke('requestOtpLogin',input.email));assert.throws(()=>f.entry.selectMode('recovery'));
 gate.resolve({error:{}});await assert.rejects(pending);assert.equal(count,1);assert.equal(f.entry.state,'ready');
});
test('disposal while login pending prevents late handoff',async()=>{
 const f=fixture(),gate=deferred();f.clients[0].signInWithOtp=()=>gate.promise;const pending=f.entry.invoke('requestOtpLogin',input.email);f.entry.dispose();
 gate.resolve({data:{}});await assert.rejects(pending);
 assert.equal(f.entry.state,'disposed');assert.deepEqual(f.sent,[]);assert.throws(()=>f.entry.selectMode('signup'));
});
test('disposal microtask at signup final read prevents handoff',async()=>{
 const f=fixture();f.entry.selectMode('signup');await f.entry.invoke('signup',input);await f.entry.invoke('verify','123456');
 const gate=deferred();let reads=0;f.clients.at(-1).getUser=()=>++reads===1?Promise.resolve({data:{user:{id}}}):gate.promise;
 const pending=f.entry.invoke('complete');while(reads<2) await Promise.resolve();
 gate.promise.then(()=>f.entry.dispose());gate.resolve({data:{user:{id}}});await assert.rejects(pending);
 assert.equal(f.entry.state,'disposed');assert.deepEqual(f.sent,[]);
});
test('failed OTP request restores mode switching and the send ledger survives the switch',async()=>{
 const f=fixture();f.clients[0].signInWithOtp=async()=>({error:{}});
 await assert.rejects(f.entry.invoke('requestOtpLogin',input.email));
 assert.equal(f.entry.state,'ready');assert(f.entry.canSwitch);
 assert.equal(f.entry.otp.sendsLeft,4);
 f.entry.selectMode('signup');assert.equal(f.entry.mode,'signup');
 f.entry.selectMode('login');assert.equal(f.entry.mode,'login');
 assert.equal(f.clients.length,3);
 // The rebuilt login controller shares the page ledger: the spent
 // send and its cooldown are still in force, so the fresh client is
 // not a budget reset.
 assert.equal(f.entry.otp.sendsLeft,4);
 await assert.rejects(f.entry.invoke('requestOtpLogin',input.email));
 assert.equal(f.entry.otp.sendsLeft,4);
});
test('an unverified OTP code step can be abandoned but a verified one cannot',async()=>{
 const f=fixture([factor]);
 await f.entry.invoke('requestOtpLogin',input.email);
 assert.equal(f.entry.state,'otp-code');assert(f.entry.canSwitch);
 f.entry.selectMode('recovery');assert.equal(f.entry.mode,'recovery');
 const g=fixture([factor]);
 await g.entry.invoke('requestOtpLogin',input.email);await g.entry.invoke('verifyOtpLogin','123456');
 assert.equal(g.entry.state,'factor-choice');assert.throws(()=>g.entry.selectMode('signup'));
});
test('restored signup after a failed login can be abandoned back to login, ledger intact',async()=>{
 const sent=[];const clients=[];
 const pendingStore={load:()=>({email:input.email,subject:id,referralCode:null}),loadFor:()=>null,save:()=>true,clear:()=>{}};
 const entry=createUnifiedEntry({origin:'https://authority.example',pendingStore,fetch:async()=>({ok:true,json:async()=>({registration_complete:true})}),handoff:async token=>{sent.push(token);},createClient:()=>{
  const user={id,factors:[]};const session={access_token:'initial-token',user:{id}};
  const auth={signUp:async()=>({data:{user}}),signInWithPassword:async()=>({data:{user,session}}),
   signInWithOtp:async()=>({error:{}}),resetPasswordForEmail:async()=>({data:{}}),
   verifyOtp:async()=>({data:{user,session}}),getSession:async()=>({data:{session}}),getUser:async()=>({data:{user}}),
   updateUser:async()=>({data:{user}}),mfa:{challenge:async()=>({data:{id:'challenge',type:'totp',expires_at:Date.now()/1000+60}}),verify:async()=>({data:{user,access_token:'rotated-token'}})}};
  clients.push(auth);return {auth};
 }});
 await assert.rejects(entry.invoke('requestOtpLogin',input.email));
 assert.equal(entry.state,'ready');assert(entry.canSwitch);
 entry.selectMode('signup');
 // The fresh signup controller restores the pending registration
 // straight into its verification step...
 assert.equal(entry.state,'verification');
 // ...but it has run nothing, so it is not trapped by the failed
 // login's history: switching back stays available.
 assert(entry.canSwitch);
 entry.selectMode('login');assert.equal(entry.mode,'login');
 // The shared OTP ledger survived both rebuilds: the failed
 // request's send and cooldown are still in force.
 assert.equal(entry.otp.sendsLeft,4);
 await assert.rejects(entry.invoke('requestOtpLogin',input.email));
 assert.equal(entry.otp.sendsLeft,4);
 assert.deepEqual(sent,[]);
});
test('unsupported verified factor is terminal without mode fallback',async()=>{
 const f=fixture([{...factor,factor_type:'phone'}]);await f.entry.invoke('requestOtpLogin',input.email);
 assert.equal((await f.entry.invoke('verifyOtpLogin','123456')).kind,'unsupported-factor');
 assert.throws(()=>f.entry.selectMode('recovery'));assert.deepEqual(f.sent,[]);
});
